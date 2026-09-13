"""DataSync：把 Tushare 的数据同步到本地。

编排规则（依据 ARCHITECTURE.md §2.5，数字全部来自第 1a 步实测）：

- **倒序**：从今天往 2016 年拉。覆盖到最近两年就够回答日常提问，更早的历史后台慢慢补。
- **并发 8 路**：代理单次调用 5~10 秒，串行只有 8 次/分钟；8 路能稳到 50 次/分钟且零失败，
  12 路会收到 `code=429 请勿使用过多线程，连接超限`。8 是实测出来的上限，不要往上调。
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

import polars as pl

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

#: 申万行业的表名。行业日线整张存不按月分片：31 个行业十年也才 8 万行上下，
#: 比股票面板一个月（11 万行）还少，分片的复杂度换不来任何好处
SW_INDUSTRY_TABLE = "meta/sw_industry"
SW_MEMBER_TABLE = "meta/sw_member"
SW_DAILY_TABLE = "board/sw_daily"

#: 指数的表名，以及 P0 要拉的宽基指数
INDEX_DAILY_TABLE = "index/daily"
INDEX_WEIGHT_TABLE = "index/weight"
BENCHMARK_INDEXES: tuple[str, ...] = (HS300, ZZ500)
# 限售解禁（share_float）P0 不同步：单个解禁日就有 2.3 万行、一个月超过 10 万行，
# 全量按天拉要十几个小时，只换来一个布尔字段。见 ARCHITECTURE §11。

#: 一个交易日的必需接口，缺一不可
REQUIRED_APIS: tuple[str, ...] = ("daily", "adj_factor", "daily_basic", "stk_limit")

#: 实测的并发上限，理由见模块文档
DEFAULT_WORKERS = 8

#: 扛不住并发的接口，串行拉。2026-09-13 实测：disclosure_date 在 8 路并发下直接返回
#: 「您请求速度过快」，串行则完全正常，而且代价极小——单次 1.5 秒、42 个报告期约 63 秒。
#: 真正需要并发的 fina_indicator_vip（单次 12.3 秒，串行要 8 分半）恰好扛得住 8 路。
SERIAL_APIS = frozenset({"disclosure_date"})


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


class DataSync:
    """按月同步日频面板。client 只需要有 `call(api_name, params)`。"""

    def __init__(
        self,
        client,
        store: MarketStore,
        workers: int = DEFAULT_WORKERS,
    ):
        self._client = client
        self._store = store
        self._workers = workers

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
        扛得住并发的接口丢进线程池，扛不住的串行（见 SERIAL_APIS）。
        限售解禁 P0 不拉，数据量的实测见 ARCHITECTURE §11。
        """
        manifest = Manifest.load(self._store) if manifest is None else manifest
        periods = report_periods(start, end)

        by_period = [
            ("fina_indicator_vip", FINA_INDICATOR_FIELDS, [{"period": p} for p in periods]),
            ("forecast_vip", FORECAST_FIELDS, [{"period": p} for p in periods]),
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

        SERIAL_APIS 里的接口单独串行——它们扛不住并发，而串行代价只有一分钟上下。
        """
        results: list[list[dict]] = [[] for _ in tasks]
        parallel = [(i, task) for i, task in enumerate(tasks) if task[0] not in SERIAL_APIS]
        serial = [(i, task) for i, task in enumerate(tasks) if task[0] in SERIAL_APIS]

        if parallel:
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                rows_iter = pool.map(lambda pair: self._client.call(*pair[1]), parallel)
                for (index, _), rows in zip(parallel, rows_iter, strict=True):
                    results[index] = rows

        for index, task in serial:
            results[index] = self._client.call(*task)
        return results

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

    def sync_index(self, start: str, end: str, manifest: Manifest | None = None) -> dict[str, int]:
        """拉宽基指数的日线与历史成分。

        日线：**一个指数一次调用就覆盖十年**（单次 8000 行，十年才 2600 个交易日）。

        成分：**按自然月拉**。接口文档自己就建议「开始日期和结束日分别输入当月第一天和
        最后一天」，而且一个月正好一页装得下（沪深300 约 300 行、中证500 约 500 行，
        单次上限 1000），这样彻底不需要翻页——这一步在翻页上栽过太多次，能不翻就不翻。

        历史成分是用来还原「当时的股票池」的：拿今天的沪深300 成分去回测 2018 年，
        等于提前知道了哪些公司会被纳入，是最典型的幸存者偏差。
        """
        manifest = Manifest.load(self._store) if manifest is None else manifest

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
                {"index_code": code, "start_date": first, "end_date": last},
                INDEX_WEIGHT_FIELDS,
            )
            for code in BENCHMARK_INDEXES
            for first, last in month_ranges(start, end)
        ]
        results = self._pull_concurrently(daily_tasks + weight_tasks)
        dailies = [row for rows in results[: len(daily_tasks)] for row in rows]
        weights = [row for rows in results[len(daily_tasks) :] for row in rows]

        written = {
            INDEX_DAILY_TABLE: self._write_table(
                INDEX_DAILY_TABLE,
                normalize_index_daily(dailies),
                manifest,
                "、".join(BENCHMARK_INDEXES),
            ),
            INDEX_WEIGHT_TABLE: self._write_table(
                INDEX_WEIGHT_TABLE, normalize_index_weight(weights), manifest, "月度快照"
            ),
        }
        manifest.save(self._store)
        logger.info("指数：%s", "，".join(f"{name} {rows} 行" for name, rows in written.items()))
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
