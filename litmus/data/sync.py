"""DataSync：把 Tushare 的数据同步到本地。

编排规则（依据 ARCHITECTURE.md §2.5，数字全部来自第 1a 步实测）：

- **倒序**：从今天往 2016 年拉。覆盖到最近两年就够回答日常提问，更早的历史后台慢慢补。
- **自适应并发**：代理单次调用 5~10 秒，串行只有 8 次/分钟，必须并发；但扛得住几路因接口而异，
  代理和配额也会变。所以不压测、不写死：撞限流减半、连续成功加一（见 tushare.py 的
  AdaptiveConcurrency）。这里的线程池大小只是天花板。
- **整月成败**：一个月的交易日全部拉齐才写文件并记账。中途失败就不落盘，下次整月重来——
  这样磁盘上永远不会出现半个月的数据（见 storage.py 的不变量）。
- **一天要么全有、要么全没有**：4 个必需接口只要有一个有数据，就必须 4 个都有。
  全都没有说明当天还没发布（比如收盘前问今天），跳过该天并把这个月记为未走完；
  只有部分有，说明数据源不一致，直接报错，不猜。
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

import polars as pl

from litmus.data.fields import CONCEPT_CAPABILITY, TARGET_CAPABILITIES
from litmus.data.loaders.concept import (
    CONCEPT_POINTS,
    normalize_tdx_daily,
    normalize_tdx_index,
    normalize_tdx_member,
)
from litmus.data.loaders.concept import DAILY_FIELDS as TDX_DAILY_FIELDS
from litmus.data.loaders.concept import INDEX_FIELDS as TDX_INDEX_FIELDS
from litmus.data.loaders.concept import MEMBER_FIELDS as TDX_MEMBER_FIELDS
from litmus.data.loaders.finance import (
    DISCLOSURE_FIELDS,
    FINA_INDICATOR_FIELDS,
    FORECAST_FIELDS,
    normalize_disclosure,
    normalize_fina_indicator,
    normalize_forecast,
)
from litmus.data.loaders.index import (
    HS300,
    INDEX_DAILY_FIELDS,
    INDEX_WEIGHT_FIELDS,
    ZZ500,
    normalize_index_daily,
    normalize_index_weight,
)
from litmus.data.loaders.industry import (
    CLASSIFY_FIELDS,
    MEMBER_FIELDS,
    SW_DAILY_FIELDS,
    SW_LEVEL,
    SW_SRC,
    normalize_index_classify,
    normalize_industry_member,
    normalize_sw_daily,
)
from litmus.data.loaders.meta import (
    LIST_STATUSES,
    NAMECHANGE_FIELDS,
    STOCK_BASIC_FIELDS,
    TRADE_CAL_FIELDS,
    normalize_namechange,
    normalize_stock_basic,
    normalize_trade_cal,
)
from litmus.data.loaders.normalize import (
    build_daily_panel,
    normalize_adj_factor,
    normalize_daily,
    normalize_daily_basic,
    normalize_stk_limit,
    normalize_stock_st,
)
from litmus.data.loaders.tushare import MAX_CONCURRENCY, TushareAuthError
from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore

logger = logging.getLogger(__name__)

#: 股票日频面板在 data/market/ 下的数据集名
DAILY_DATASET = "daily"

#: 基础数据的表名（不按月分片，每次同步整张覆盖）
STOCK_BASIC_TABLE = "meta/stock_basic"
TRADE_CAL_TABLE = "meta/trade_cal"
NAMECHANGE_TABLE = "meta/namechange"

#: 财务与事件的表名。事件是稀疏标记，不进面板，单独放 events/ 下
FINA_INDICATOR_TABLE = "fina_indicator"
DISCLOSURE_TABLE = "events/disclosure"
FORECAST_TABLE = "events/forecast"
# 限售解禁（share_float）P0 不同步：单个解禁日就有 2.3 万行、一个月超过 10 万行，
# 全量按天拉要十几个小时，只换来一个布尔字段。见 ARCHITECTURE §11。

#: 申万行业的表名。行业日线整张存不按月分片：31 个行业十年也才 8 万行上下，
#: 比股票面板一个月（11 万行）还少，分片的复杂度换不来任何好处
SW_INDUSTRY_TABLE = "meta/sw_industry"
SW_MEMBER_TABLE = "meta/sw_member"
SW_DAILY_TABLE = "board/sw_daily"

#: 通达信概念板块的表名。前缀 tdx_ 标明口径——申万那组是 sw_，以后换同花顺也不会混
TDX_CONCEPT_TABLE = "meta/tdx_concept"
TDX_MEMBER_TABLE = "meta/tdx_member"
TDX_DAILY_TABLE = "board/tdx_daily"

#: 找概念板块清单时最多往回试几个交易日。清单按交易日发布，收盘前同步时当天的还没有，
#: 往前退一天就有；连续这么多天都是空的，就不是「还没发布」，而是出了别的问题
SNAPSHOT_LOOKBACK_DAYS = 5

#: 指数日线是整张表；成分按月分片记账（和股票日频同一套），增量同步只拉没走完的月份
INDEX_DAILY_TABLE = "index/daily"
INDEX_WEIGHT_DATASET = "index/weight"
BENCHMARK_INDEXES: tuple[str, ...] = (HS300, ZZ500)
#: 指数成分有发布滞后：2026-09-13 实测最新快照是 8/31，9 月的窗口返回 0 行，
#: 月末那期也可能下个月才发。所以最近这几个月每次同步都重拉，更早的月份拉到了才算走完
INDEX_WEIGHT_UNSETTLED_MONTHS = 2

#: 一个交易日的必需接口，缺一不可
REQUIRED_APIS: tuple[str, ...] = ("daily", "adj_factor", "daily_basic", "stk_limit")

#: 同步的六个步骤，**按这个顺序执行**。daily 放最后：前面几步加起来几分钟，
#: 它一个人要一个多小时，先让便宜的都就位。concept 紧跟 industry，两种板块口径挨着。
SYNC_STEPS: tuple[str, ...] = ("meta", "industry", "concept", "index", "finance", "daily")

#: 历史数据的起点：ST 名单接口 stock_st 从 2016 年起才有数据（ARCHITECTURE §2.5）
HISTORY_START = "20160101"

#: 股票日频从本地最新一天往回覆盖满这么多年，就可以开始提问，更早的历史后台接着补
UNLOCK_YEARS = 2

#: 能提问之前必须就位的表：日频之前那几步写的。指数成分和指数日线同一步落盘、同一次记账，
#: 有日线就有成分。概念板块是可选数据，可用时才要求 CONCEPT_TABLES
REQUIRED_TABLES: tuple[str, ...] = (
    STOCK_BASIC_TABLE,
    TRADE_CAL_TABLE,
    NAMECHANGE_TABLE,
    SW_INDUSTRY_TABLE,
    SW_MEMBER_TABLE,
    SW_DAILY_TABLE,
    INDEX_DAILY_TABLE,
    FINA_INDICATOR_TABLE,
    DISCLOSURE_TABLE,
    FORECAST_TABLE,
)
CONCEPT_TABLES: tuple[str, ...] = (TDX_CONCEPT_TABLE, TDX_MEMBER_TABLE, TDX_DAILY_TABLE)


def report_periods(start: str, end: str) -> list[str]:
    """[start, end] 覆盖到的报告期（每个季度最后一天），从早到晚。

    财务和预告都按报告期拉，不按交易日：十年只有四十来个报告期，
    而交易日有两千多个。还没到的报告期自然落在 end 之后，不会被拉。
    """
    periods = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        for quarter_end in ("0331", "0630", "0930", "1231"):
            period = f"{year}{quarter_end}"
            if start <= period <= end:
                periods.append(period)
    return periods


def next_report_period(day: str) -> str:
    """day 之后（不含当天）的第一个报告期。"""
    year = int(day[:4])
    for quarter_end in ("0331", "0630", "0930", "1231"):
        if f"{year}{quarter_end}" > day:
            return f"{year}{quarter_end}"
    return f"{year + 1}0331"


def month_window(month: str) -> tuple[str, str]:
    """整月的起止日，形如 "2024-02" → ("20240201", "20240229")。"""
    year, mon = int(month[:4]), int(month[5:])
    return f"{year}{mon:02d}01", f"{year}{mon:02d}{calendar.monthrange(year, mon)[1]:02d}"


def month_ranges(start: str, end: str) -> list[tuple[str, str]]:
    """[start, end] 覆盖到的自然月区间，形如 ("20240101", "20240131")。

    首尾两个月按 start / end 裁剪，不会越界。指数成分按月拉就靠它。
    """
    ranges: list[tuple[str, str]] = []
    year, month = int(start[:4]), int(start[4:6])
    while f"{year}{month:02d}01" <= end:
        last_day = calendar.monthrange(year, month)[1]
        first = max(f"{year}{month:02d}01", start)
        last = min(f"{year}{month:02d}{last_day:02d}", end)
        ranges.append((first, last))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return ranges


def months_between(start: str, end: str) -> tuple[str, ...]:
    """[start, end] 覆盖到的自然月，从早到晚，形如 ("2024-01", "2024-02")。"""
    return tuple(f"{first[:4]}-{first[4:6]}" for first, _ in month_ranges(start, end))


class SyncError(RuntimeError):
    """同步中断。已经落盘的月份不受影响，重跑会从断点继续。"""


@dataclass(frozen=True)
class MonthResult:
    """一个月同步完的结果。`complete=False` 表示这个月还会再拉一次。"""

    month: str
    rows: int
    days: int
    skipped_days: tuple[str, ...]
    complete: bool


ProgressFn = Callable[[MonthResult, int, int], None]


@dataclass(frozen=True)
class DataStatus:
    """本地数据到了什么程度，全部从 manifest 和文件推导（见 DataSync.status）。"""

    #: 能不能开始提问；不能时 reason 说明缺什么
    ready: bool
    reason: str
    #: 股票日频最新一天，页面上的「数据截至」
    data_through: str | None
    #: 从最新一天往回连续覆盖到哪天，页面上的「历史已补到」
    history_from: str | None
    #: 是否已经连续补到 HISTORY_START
    history_done: bool
    #: 解锁要求落盘的月份，以及其中还缺的——data_not_ready 附带的进度
    unlock_months: tuple[str, ...]
    unlock_missing: tuple[str, ...]
    #: 各表、各按月数据集最近一次同步的时间
    synced_at: dict[str, str]
    #: 用不了的可选数据：能力名 → 原因。不在这里的就是可用
    unavailable: dict[str, str]


class DataSync:
    """把 Tushare 的数据同步到本地。

    client 要有 `call()`；同步概念板块时还要 `probe()` 做能力探测。只看状态（status）可以传 None。
    """

    def __init__(
        self,
        client,
        store: MarketStore,
        workers: int = MAX_CONCURRENCY,
    ):
        self._client = client
        self._store = store
        self._workers = workers

    # ── 状态 ────────────────────────────────────────────────────

    def status(self, manifest: Manifest | None = None) -> DataStatus:
        """本地数据到了什么程度、能不能开始提问。只读 manifest 和文件，不联网。

        能提问要同时满足两条（ARCHITECTURE §2.5「解锁判定」）：

        - **股票日频从本地最新一天往回 UNLOCK_YEARS 年，经过的每个自然月都已落盘**。最新那个月
          可以没走完，其余必须整月，记了账但文件不在的算缺。锚在数据的最新一天而不是今天：
          月初收盘前、长假里当月还没有数据，锚在今天会误判；数据旧了照样放行，由页面提示去同步。
          A 股每个自然月都有交易日，所以不用查日历
        - **日频之前那几步的表都在**，概念板块可用时它的表也要在。否则只跑过 `--only daily`
          的目录也会放行，查询时才发现缺表

        「历史是否补完」同样从月份推导，manifest 不另存标记——标记会和文件对不上。
        同步中的实时进度等第 5 步有了后台任务再加。
        """
        manifest = Manifest.load(self._store) if manifest is None else manifest
        records = manifest.months.get(DAILY_DATASET, {})
        newest = max(records, default=None)

        def on_disk(month: str) -> bool:
            record = records.get(month)
            return (
                record is not None
                and (record.complete or month == newest)
                and self._store.has_month(DAILY_DATASET, month)
            )

        through = manifest.data_through(DAILY_DATASET)
        unlock_months: tuple[str, ...] = ()
        history_from: str | None = None
        history_done = False
        if through is not None:
            through_day = through.replace("-", "")
            years_ago = f"{int(through_day[:4]) - UNLOCK_YEARS}{through_day[4:6]}01"
            unlock_months = months_between(years_ago, through_day)
            # 从最新的月份往回数，数到第一个缺口为止
            history = months_between(HISTORY_START, through_day)
            contiguous = 0
            for month in reversed(history):
                if not on_disk(month):
                    break
                contiguous += 1
            if contiguous:
                history_from = records[history[-contiguous]].first_date
                history_done = contiguous == len(history)
        unlock_missing = tuple(month for month in unlock_months if not on_disk(month))

        required = REQUIRED_TABLES
        if manifest.is_available(CONCEPT_CAPABILITY):
            required += CONCEPT_TABLES
        missing_tables = [
            name
            for name in required
            if name not in manifest.tables or not self._store.has_table(name)
        ]

        reasons: list[str] = []
        if through is None:
            reasons.append("还没有股票日频数据")
        elif unlock_missing:
            shown = "、".join(unlock_missing[:3]) + (" 等" if len(unlock_missing) > 3 else "")
            reasons.append(
                f"最近 {UNLOCK_YEARS} 年的股票日频还缺 {len(unlock_missing)} 个月（{shown}）"
            )
        if missing_tables:
            reasons.append(f"缺少 {'、'.join(missing_tables)}")

        synced_at = {name: record.synced_at for name, record in manifest.tables.items()}
        for dataset, months in manifest.months.items():
            if months:
                synced_at[dataset] = max(record.synced_at for record in months.values())

        return DataStatus(
            ready=not reasons,
            reason="；".join(reasons),
            data_through=through,
            history_from=history_from,
            history_done=history_done,
            unlock_months=unlock_months,
            unlock_missing=unlock_missing,
            synced_at=synced_at,
            unavailable={
                name: manifest.unavailable_reason(name)
                for name in TARGET_CAPABILITIES.values()
                if not manifest.is_available(name)
            },
        )

    # ── 交易日历 ────────────────────────────────────────────────

    def trading_days(self, start: str, end: str) -> dict[str, list[str]]:
        """按月分组的交易日，形如 {"2026-08": ["20260803", ...]}。"""
        rows = self._client.call(
            "trade_cal",
            {"exchange": "SSE", "start_date": start, "end_date": end, "is_open": "1"},
        )
        by_month: dict[str, list[str]] = {}
        for row in rows:
            day = str(row["cal_date"])
            by_month.setdefault(f"{day[:4]}-{day[4:6]}", []).append(day)
        return {month: sorted(days) for month, days in sorted(by_month.items())}

    # ── 同步 ────────────────────────────────────────────────────

    def sync_all(
        self,
        start: str,
        end: str,
        manifest: Manifest | None = None,
        steps: Sequence[str] | None = None,
        on_month: ProgressFn | None = None,
    ) -> dict[str, object]:
        """按固定顺序跑完选中的同步步骤。

        `steps` 只用来**筛选**，不决定顺序——顺序永远是 SYNC_STEPS。
        传 `["daily", "meta"]` 也会先跑 meta 再跑 daily，因为顺序是有依赖含义的，
        不该由命令行的打字顺序决定。
        """
        manifest = Manifest.load(self._store) if manifest is None else manifest
        chosen = set(steps) if steps else set(SYNC_STEPS)
        unknown = chosen - set(SYNC_STEPS)
        if unknown:
            raise SyncError(f"不认识的同步步骤：{sorted(unknown)}；可选 {list(SYNC_STEPS)}")

        summary: dict[str, object] = {}
        for step in SYNC_STEPS:
            if step not in chosen:
                continue
            logger.info("=== %s ===", step)
            if step == "daily":
                months = self.sync_daily(start, end, manifest, on_month)
                summary[step] = {
                    "months": len(months),
                    "rows": sum(result.rows for result in months),
                }
            elif step == "meta":
                summary[step] = self.sync_meta(start, end, manifest)
            elif step == "industry":
                summary[step] = self.sync_industry(start, end, manifest)
            elif step == "concept":
                summary[step] = self.sync_concept(start, end, manifest)
            elif step == "index":
                summary[step] = self.sync_index(start, end, manifest)
            elif step == "finance":
                summary[step] = self.sync_finance(start, end, manifest)
        return summary

    def sync_meta(self, start: str, end: str, manifest: Manifest | None = None) -> dict[str, int]:
        """拉基础数据：股票列表（含退市）、交易日历、曾用名。

        这三张表都很小，每次同步整张覆盖，不做增量——股票会改名、会退市，
        增量合并反而容易留下过期的行。
        """
        manifest = Manifest.load(self._store) if manifest is None else manifest

        # 三种上市状态分别拉：只拉 L 会把退市股排除在外，历史回测就成了幸存者偏差
        listings: list[dict] = []
        for status in LIST_STATUSES:
            listings.extend(
                self._client.call("stock_basic", {"list_status": status}, STOCK_BASIC_FIELDS)
            )

        calendar = self._client.call(
            "trade_cal",
            {"exchange": "SSE", "start_date": start, "end_date": end},
            TRADE_CAL_FIELDS,
        )
        names = self._client.call("namechange", {}, NAMECHANGE_FIELDS)

        written = {
            STOCK_BASIC_TABLE: self._write_table(
                STOCK_BASIC_TABLE, normalize_stock_basic(listings), manifest, "含退市与暂停上市"
            ),
            TRADE_CAL_TABLE: self._write_table(
                TRADE_CAL_TABLE, normalize_trade_cal(calendar), manifest, f"{start}~{end}"
            ),
            NAMECHANGE_TABLE: self._write_table(
                NAMECHANGE_TABLE, normalize_namechange(names), manifest
            ),
        }
        manifest.save(self._store)
        logger.info("基础数据：%s", "，".join(f"{k} {v} 行" for k, v in written.items()))
        return written

    def _write_table(
        self, name: str, table: pl.DataFrame, manifest: Manifest, note: str = ""
    ) -> int:
        if table.is_empty():
            raise SyncError(f"{name} 一行都没拉到，不覆盖已有的表")
        self._store.write_table(name, table)
        manifest.record_table(name, table, note)
        return table.height

    def sync_finance(
        self, start: str, end: str, manifest: Manifest | None = None
    ) -> dict[str, int]:
        """拉财务指标与两类事件（财报披露、业绩预告）。

        它们都不进日频面板，各自落一张表，读取时再按 PIT 规则拼上去（见 finance.py）。
        三个接口一起丢进线程池，各自能并发几路由 client 的自适应并发决定。
        限售解禁 P0 不拉，数据量的实测见 ARCHITECTURE §11。

        **每次整张重拉，不做增量**：2026-09-13 实测财务指标约 7% 的行是公司上市后补报的
        历史财务，公告日比报告期末晚一年以上（最长近 4 年）。只重拉最近几个报告期会漏掉它们；
        按公告日区间拉倒是能收全，但接口文档把这两个参数写成「报告期」，行为和文档对不上，
        不值得为省两分钟去赌。
        """
        manifest = Manifest.load(self._store) if manifest is None else manifest
        periods = report_periods(start, end)
        # 业绩预告在报告期结束**之前**就发（三季报预告 9 月就有），所以多拉 end 之后的下一个报告期。
        # 2026-09-13 实测：8/1~9/13 公告的预告里有 30 条属于 20260930，只拉「报告期 <= end」会全漏
        forecast_periods = [*periods, next_report_period(end)]

        by_period = [
            ("fina_indicator_vip", FINA_INDICATOR_FIELDS, [{"period": p} for p in periods]),
            ("forecast_vip", FORECAST_FIELDS, [{"period": p} for p in forecast_periods]),
            ("disclosure_date", DISCLOSURE_FIELDS, [{"end_date": p} for p in periods]),
        ]
        tasks = [
            (api_name, params, fields)
            for api_name, fields, param_list in by_period
            for params in param_list
        ]
        results = self._pull_concurrently(tasks)

        # 按各接口的请求条数把结果切回去
        chunks: list[list[dict]] = []
        cursor = 0
        for _, _, param_list in by_period:
            merged = [row for rows in results[cursor : cursor + len(param_list)] for row in rows]
            chunks.append(merged)
            cursor += len(param_list)
        fina, forecast, disclosure = chunks

        written = {
            FINA_INDICATOR_TABLE: self._write_table(
                FINA_INDICATOR_TABLE,
                normalize_fina_indicator(fina),
                manifest,
                f"{periods[0]}~{periods[-1]}" if periods else "",
            ),
            FORECAST_TABLE: self._write_table(
                FORECAST_TABLE, normalize_forecast(forecast), manifest
            ),
            DISCLOSURE_TABLE: self._write_table(
                DISCLOSURE_TABLE, normalize_disclosure(disclosure), manifest
            ),
        }
        manifest.save(self._store)
        logger.info(
            "财务与事件（%d 个报告期）：%s",
            len(periods),
            "，".join(f"{name} {rows} 行" for name, rows in written.items()),
        )
        return written

    def _pull_concurrently(self, tasks: Sequence[tuple[str, dict, str | None]]) -> list[list[dict]]:
        """跑一批 (接口, 参数, 字段)，按传入顺序返回。任何一个失败就整体抛错。

        同时在飞几个由 client 的自适应并发决定：扛不住并发的接口（实测 disclosure_date）
        撞几次限流就自己降到一两路，不用在这里点名串行。
        """
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            return list(pool.map(lambda task: self._client.call(*task), tasks))

    def sync_industry(
        self, start: str, end: str, manifest: Manifest | None = None
    ) -> dict[str, int]:
        """拉申万一级行业：清单、历史归属、行业日线。

        先取清单拿到 31 个行业代码，再按行业并发拉归属和日线。两者都按行业代码取，
        **一个行业一次调用就覆盖十年**——`sw_daily` 单次 4000 行，而十年只有 2600 个交易日。

        `is_new` 是**过滤器**，不是「包含历史」的开关：`Y` 只返回当前成分，`N` 只返回
        已经调出的，所以**两个都要拉**。只拉 Y 会丢掉历史（换过行业的公司全按今天的归属算，
        是最隐蔽的幸存者偏差）；只拉 N 会丢掉现在——2026-09-13 实测 `is_new='N'` 返回的
        2011 行**全部带 out_date**，一条当前成分都没有，那样行业筛选会全空。
        """
        manifest = Manifest.load(self._store) if manifest is None else manifest

        classify = normalize_index_classify(
            self._client.call("index_classify", {"level": SW_LEVEL, "src": SW_SRC}, CLASSIFY_FIELDS)
        )
        codes = classify.get_column("code").to_list()
        if not codes:
            raise SyncError("申万行业清单是空的，归属和日线无从拉起")

        # Y 只给当前成分、N 只给已调出的，两个都要，缺一边都是错的
        member_tasks = [
            ("index_member_all", {"l1_code": code, "is_new": flag}, MEMBER_FIELDS)
            for code in codes
            for flag in ("Y", "N")
        ]
        daily_tasks = [
            ("sw_daily", {"ts_code": code, "start_date": start, "end_date": end}, SW_DAILY_FIELDS)
            for code in codes
        ]
        results = self._pull_concurrently(member_tasks + daily_tasks)
        members = [row for rows in results[: len(member_tasks)] for row in rows]
        dailies = [row for rows in results[len(member_tasks) :] for row in rows]

        written = {
            SW_INDUSTRY_TABLE: self._write_table(
                SW_INDUSTRY_TABLE, classify, manifest, f"申万 {SW_SRC} 一级"
            ),
            SW_MEMBER_TABLE: self._write_table(
                SW_MEMBER_TABLE,
                normalize_industry_member(members),
                manifest,
                "含已调出的成分（is_new=N）",
            ),
            SW_DAILY_TABLE: self._write_table(
                SW_DAILY_TABLE, normalize_sw_daily(dailies), manifest, f"{start}~{end}"
            ),
        }
        manifest.save(self._store)
        logger.info(
            "申万行业：%s", "，".join(f"{name} {rows} 行" for name, rows in written.items())
        )
        return written

    def sync_concept(
        self, start: str, end: str, manifest: Manifest | None = None
    ) -> dict[str, object]:
        """拉通达信概念板块：清单、当前成分、板块日线。三个接口都要 6000 积分。

        **先做能力探测**：没权限就记为不可用并跳过，不报错，返回 `{"skipped": 原因}`——
        5000 积分的用户照样能同步其余数据，概念板块也不会出现在可用标的里
        （见 fields.available_targets）。token 填错不算没权限，照常报错。

        形状和 sync_industry 一样：先取清单，再按板块并发拉成分和日线。拉法由
        2026-09-13 的实测决定：

        - **清单必须带交易日**：不带就是按日往回翻历史，非交易日返回 0 行。所以先定快照日——
          end 之前最近一个有清单的交易日
        - **成分只能按板块拉**：一天全部板块的成分约 8.4 万行，按 3000 行一页要 28 页，
          超过 MAX_PAGES，也逼近代理的 offset 上限；按板块拉，一个板块一页就够
        - **日线一个板块一次调用覆盖全部历史**：三个接口都从 2025-03-28 才有数据，
          一个板块才三百多行

        **只拉快照日还在的板块**：已经撤销的概念板块（实测一年半撤了 5 个，如「新冠检测」）
        的历史不会落盘，历史上某一天的板块排行里没有它们。P0 的概念板块本来就是
        「当前视角」（成分也只有当前快照），这个限制见 ARCHITECTURE §2.7。
        """
        manifest = Manifest.load(self._store) if manifest is None else manifest

        # 能力探测只试 tdx_index：每个概念板块问题都要靠清单把「光模块」这种名字认成板块代码，
        # 而三个接口同属 6000 积分档，有它就有全部
        available, reason = self._client.probe("tdx_index")
        if not available:
            return self._skip_concept(manifest, reason)
        manifest.record_capability(CONCEPT_CAPABILITY, True)

        try:
            snapshot, index_rows = self._latest_concept_index(end)
            concepts = normalize_tdx_index(index_rows)
            codes = concepts.get_column("code").to_list()
            if not codes:
                raise SyncError(f"{snapshot} 的通达信板块清单里没有概念板块，成分和日线无从拉起")

            member_tasks = [
                ("tdx_member", {"ts_code": code, "trade_date": snapshot}, TDX_MEMBER_FIELDS)
                for code in codes
            ]
            daily_tasks = [
                (
                    "tdx_daily",
                    {"ts_code": code, "start_date": start, "end_date": end},
                    TDX_DAILY_FIELDS,
                )
                for code in codes
            ]
            results = self._pull_concurrently(member_tasks + daily_tasks)
        except TushareAuthError as exc:
            # 兜底：代理可能按接口单独配权限，清单开了、成分或日线没开，探测发现不了。
            # 照样记为不可用并跳过，别让整个同步断在这一步
            return self._skip_concept(manifest, str(exc))
        members = [row for rows in results[: len(member_tasks)] for row in rows]
        dailies = normalize_tdx_daily(
            [row for rows in results[len(member_tasks) :] for row in rows]
        )
        dates = dailies.get_column("date")

        written = {
            TDX_CONCEPT_TABLE: self._write_table(
                TDX_CONCEPT_TABLE, concepts, manifest, f"通达信概念板块，{snapshot} 快照"
            ),
            TDX_MEMBER_TABLE: self._write_table(
                TDX_MEMBER_TABLE,
                normalize_tdx_member(members),
                manifest,
                f"{snapshot} 的当前成分，不是历史成分",
            ),
            # 备注写实际区间而不是请求区间：请求从 2016 年起，数据其实从 2025-03-28 才有
            TDX_DAILY_TABLE: self._write_table(
                TDX_DAILY_TABLE, dailies, manifest, f"{dates.min()}~{dates.max()}"
            ),
        }
        manifest.save(self._store)
        logger.info(
            "概念板块（%s 快照）：%s",
            snapshot,
            "，".join(f"{name} {rows} 行" for name, rows in written.items()),
        )
        return written

    def _skip_concept(self, manifest: Manifest, reason: str) -> dict[str, object]:
        """没权限：记为不可用、马上存盘，然后跳过。

        马上存盘是因为后面的步骤可能中断，页面要靠这条记录告诉用户为什么没有概念板块。
        已经落盘的旧表不删——标成不可用，上层就不会读它。
        """
        reason = f"需要 {CONCEPT_POINTS} 积分：{reason}"
        manifest.record_capability(CONCEPT_CAPABILITY, False, reason)
        manifest.save(self._store)
        logger.warning("概念板块不可用，跳过：%s", reason)
        return {"skipped": reason}

    def _latest_concept_index(self, end: str) -> tuple[str, list[dict]]:
        """end 之前最近一个有概念板块清单的交易日，连同那天的清单一起返回。

        清单按交易日发布：非交易日没有，收盘前同步时当天的也还没有，所以从最近的交易日往回试。
        """
        window_start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=31)).strftime("%Y%m%d")
        recent = sorted(
            (day for days in self.trading_days(window_start, end).values() for day in days),
            reverse=True,
        )
        for day in recent[:SNAPSHOT_LOOKBACK_DAYS]:
            rows = self._client.call("tdx_index", {"trade_date": day}, TDX_INDEX_FIELDS)
            if rows:
                return day, rows
        raise SyncError(f"{end} 之前最近 {SNAPSHOT_LOOKBACK_DAYS} 个交易日都没有通达信板块清单")

    def sync_index(self, start: str, end: str, manifest: Manifest | None = None) -> dict[str, int]:
        """拉宽基指数的日线与历史成分。

        日线：**一个指数一次调用就覆盖十年**（单次 8000 行，十年才 2600 个交易日），每次整张重拉。

        成分：**按自然月拉、按月落盘记账**，和股票日频同一套，增量同步只拉没走完的月份。
        接口文档自己就建议「开始日期和结束日分别输入当月第一天和最后一天」，而且一个月正好
        一页装得下（沪深300 一个月 1~2 个快照、中证500 一个，单次上限 1000），这样彻底不需要
        翻页——这一步在翻页上栽过太多次，能不翻就不翻。窗口永远取整月、不按 start / end 裁剪：
        月文件要么不存在，要么是整月。

        历史成分是用来还原「当时的股票池」的：拿今天的沪深300 成分去回测 2018 年，
        等于提前知道了哪些公司会被纳入，是最典型的幸存者偏差。
        """
        manifest = Manifest.load(self._store) if manifest is None else manifest

        months = months_between(start, end)
        unsettled = set(months[-INDEX_WEIGHT_UNSETTLED_MONTHS:])
        todo = manifest.missing_months(INDEX_WEIGHT_DATASET, months, self._store)

        daily_tasks = [
            (
                "index_daily",
                {"ts_code": code, "start_date": start, "end_date": end},
                INDEX_DAILY_FIELDS,
            )
            for code in BENCHMARK_INDEXES
        ]
        weight_tasks = [
            (
                "index_weight",
                {
                    "index_code": code,
                    "start_date": month_window(month)[0],
                    "end_date": month_window(month)[1],
                },
                INDEX_WEIGHT_FIELDS,
            )
            for month in todo
            for code in BENCHMARK_INDEXES
        ]
        results = self._pull_concurrently(daily_tasks + weight_tasks)
        dailies = [row for rows in results[: len(daily_tasks)] for row in rows]

        # 先全部检查完再落盘：早就该有快照的月份空了，说明数据源出了问题，一个月都不写
        by_month: dict[str, list[dict]] = {month: [] for month in todo}
        for (_, params, _), rows in zip(weight_tasks, results[len(daily_tasks) :], strict=True):
            month = f"{params['start_date'][:4]}-{params['start_date'][4:6]}"
            if not rows and month not in unsettled:
                raise SyncError(
                    f"{params['index_code']} 在 {month} 一个成分快照都没有，不正常；"
                    "实测 2016 年以来每个月两个指数都有快照"
                )
            by_month[month].extend(rows)

        written = {
            INDEX_DAILY_TABLE: self._write_table(
                INDEX_DAILY_TABLE,
                normalize_index_daily(dailies),
                manifest,
                "、".join(BENCHMARK_INDEXES),
            ),
            INDEX_WEIGHT_DATASET: 0,
        }
        for month, rows in by_month.items():
            if not rows:
                continue  # 最近的月份还没发布：不写空文件、不记账，下次同步再拉
            table = normalize_index_weight(rows)
            self._store.write_month(INDEX_WEIGHT_DATASET, month, table)
            manifest.record_month(
                INDEX_WEIGHT_DATASET, month, table, complete=month not in unsettled
            )
            written[INDEX_WEIGHT_DATASET] += table.height
        manifest.save(self._store)
        logger.info(
            "指数：日线 %d 行；成分待拉 %d 个月（共 %d 个月），写入 %d 行",
            written[INDEX_DAILY_TABLE],
            len(todo),
            len(months),
            written[INDEX_WEIGHT_DATASET],
        )
        return written

    def sync_daily(
        self,
        start: str,
        end: str,
        manifest: Manifest | None = None,
        on_month: ProgressFn | None = None,
    ) -> list[MonthResult]:
        """同步 [start, end] 的日频面板，**从最近的月份往回拉**。

        每完成一个月就存一次 manifest：中途断电，已完成的月份不用重来。
        """
        manifest = Manifest.load(self._store) if manifest is None else manifest
        # 交易日历按整月取：月文件要么不存在要么是整月，请求区间截在月中也不能只落半个月
        by_month = self.trading_days(f"{start[:6]}01", f"{end[:6]}31")
        newest_first = sorted(by_month, reverse=True)
        todo = manifest.missing_months(DAILY_DATASET, newest_first, self._store)
        # 账上已有、只是没走完（或文件丢了）的月份先补，再拉新月份。跨月那次同步要是先写下 10 月，
        # 9 月月末那几天在 9 月补完之前就是夹在中间的缺口，status() 会退回「不能提问」
        todo = sorted(todo, key=lambda month: manifest.month(DAILY_DATASET, month) is None)

        # 交易日历是提前发布的，end 之后的交易日还没有数据，不用白调
        plan: list[tuple[str, list[str], bool]] = []
        for month in todo:
            all_days = by_month[month]
            days = [day for day in all_days if day <= end]
            if days:
                plan.append((month, days, len(days) == len(all_days)))
        logger.info("待同步 %d 个月（共 %d 个月），从 %s 往回拉", len(plan), len(by_month), end)

        results: list[MonthResult] = []
        for index, (month, days, whole_month) in enumerate(plan, start=1):
            result = self.sync_month(month, days, manifest, whole_month=whole_month)
            manifest.save(self._store)
            results.append(result)
            logger.info(
                "%s 完成：%d 行 / %d 天%s",
                month,
                result.rows,
                result.days,
                "" if result.complete else f"（跳过 {len(result.skipped_days)} 天，未走完）",
            )
            if on_month is not None:
                on_month(result, index, len(plan))
        return results

    def sync_month(
        self,
        month: str,
        days: Sequence[str],
        manifest: Manifest,
        *,
        whole_month: bool = True,
    ) -> MonthResult:
        """拉一个月。任何一次调用失败都会抛错，这个月不落盘。

        `whole_month=False` 表示 days 只是这个月交易日的一部分（请求区间截断，或者月还没过完），
        这时即使拉全了也不算走完，下次同步会把整月重来一遍。
        """
        pulled = self._pull_days(days)
        # ST 名单按日期区间拉：一个月一次就够，不必按天，省下二十来倍的调用
        st = normalize_stock_st(
            self._client.call("stock_st", {"start_date": days[0], "end_date": days[-1]})
        )

        frames: list[pl.DataFrame] = []
        skipped: list[str] = []
        for day in days:
            per_api = pulled[day]
            missing = [api for api in REQUIRED_APIS if not per_api[api]]
            if len(missing) == len(REQUIRED_APIS):
                skipped.append(day)  # 当天还没发布
                continue
            if missing:
                raise SyncError(f"{day} 只拿到部分接口的数据，缺 {missing}；{month} 不落盘")
            frames.append(
                build_daily_panel(
                    normalize_daily(per_api["daily"]),
                    normalize_adj_factor(per_api["adj_factor"]),
                    normalize_daily_basic(per_api["daily_basic"]),
                    normalize_stk_limit(per_api["stk_limit"]),
                    st,
                )
            )

        if frames and st.is_empty():
            raise SyncError(f"{month} 一个 ST 都没拉到，不正常；宁可停下也不把全市场标成非 ST")

        if not frames:
            # 整月都还没数据（比如刚开月、还没开盘），不写空文件
            return MonthResult(month, 0, 0, tuple(skipped), complete=False)

        panel = pl.concat(frames)
        self._store.write_month(DAILY_DATASET, month, panel)
        complete = whole_month and not skipped
        manifest.record_month(DAILY_DATASET, month, panel, complete=complete)
        return MonthResult(
            month=month,
            rows=panel.height,
            days=panel.get_column("date").n_unique(),
            skipped_days=tuple(skipped),
            complete=complete,
        )

    def _pull_days(self, days: Sequence[str]) -> dict[str, dict[str, list[dict]]]:
        """并发拉取一个月的所有 (交易日 × 接口)。任何一个失败就整体抛错。"""
        tasks = [(day, api) for day in days for api in REQUIRED_APIS]
        pulled: dict[str, dict[str, list[dict]]] = {day: {} for day in days}
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            rows_per_task = pool.map(self._pull_one, tasks)
            for (day, api), rows in zip(tasks, rows_per_task, strict=True):
                pulled[day][api] = rows
        return pulled

    def _pull_one(self, task: tuple[str, str]) -> list[dict]:
        day, api_name = task
        return self._client.call(api_name, {"trade_date": day})
