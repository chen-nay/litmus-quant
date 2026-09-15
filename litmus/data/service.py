"""DataService：上层读本地数据的唯一入口。只读本地 Parquet，从不联网。

表达式引擎、研究计算只问它两类问题：「这些标的在这段时间的这些字段是多少」「某一天的股票池里有谁」。
A 股数据的坑——后复权、财务按公告日对齐、停牌、ST、次新、指数与行业的历史成分——在这一层一次处理完，
上层不碰文件路径，也不知道数据来自哪个数据源：换数据源只换 loader，这里的接口不动。

**不做缓存**，每次按需读文件。2026-09-14 本机实测：全市场 37 个月 4 列 396 万行 0.03 秒，
单只股票 10 年 0.06 秒。缓存省不下什么，反而要处理「同步已经改了文件、服务里还是旧数据」。

口径见 ARCHITECTURE.md §2.1、§2.6；契约测试在 tests/contract/test_dataservice.py。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import polars as pl

from litmus.data.derive import (
    FINANCE_FIELDS,
    finance_timeline,
    with_event,
    with_ex_div,
    with_finance,
    with_is_new,
)
from litmus.data.fields import (
    CONCEPT,
    INTERNAL_COLUMNS,
    STOCK,
    SW_INDUSTRY,
    TARGET_CAPABILITIES,
    available_targets,
    check_available,
)
from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore, MissingDataError
from litmus.data.sync import (
    BENCHMARK_INDEXES,
    DAILY_DATASET,
    DISCLOSURE_TABLE,
    FINA_INDICATOR_TABLE,
    FORECAST_TABLE,
    INDEX_DAILY_TABLE,
    INDEX_WEIGHT_DATASET,
    NAMECHANGE_TABLE,
    STOCK_BASIC_TABLE,
    SW_DAILY_TABLE,
    SW_INDUSTRY_TABLE,
    SW_MEMBER_TABLE,
    TDX_CONCEPT_TABLE,
    TDX_DAILY_TABLE,
    TDX_MEMBER_TABLE,
    TRADE_CAL_TABLE,
    DataSync,
    months_between,
)
from litmus.data.universe import BASES, industry_members, industry_of, universe_mask

#: 读的时候现算、不在日频面板里的股票字段（规则见 derive.py）
DERIVED_FIELDS = frozenset(
    {*FINANCE_FIELDS, "is_report_date", "is_forecast_date", "is_ex_div", "is_new"}
)

#: 要和每只股票的上一条行情比才算得出来的字段
_NEEDS_PREVIOUS_ROW = frozenset({"is_report_date", "is_forecast_date", "is_ex_div"})

#: 公告日事件字段 → (表, 公告日所在的列)。财报披露看实际披露日，预计披露日会变
_EVENT_SOURCES: dict[str, tuple[str, str]] = {
    "is_report_date": (DISCLOSURE_TABLE, "actual_date"),
    "is_forecast_date": (FORECAST_TABLE, "ann_date"),
}

#: 板块标的 → 板块日线表
_BOARD_DAILY: dict[str, str] = {SW_INDUSTRY: SW_DAILY_TABLE, CONCEPT: TDX_DAILY_TABLE}

#: 涨跌停相关的布尔列
_LIMIT_FLAGS = frozenset({"is_limit_up", "is_limit_down", "open_limit_up"})

#: 涨停价不低于这个数，说明当天不设涨跌幅限制。2026-09-14 实测上市首日、退市整理期这类日子，数据源把涨停价填成
#: 99999.999、100000、999999.999、1000000（共 6886 行，其中 115 行跌停价为空，跌停标记跟着成了空值）。
#: A 股没有接近 1 万元的股价
NO_LIMIT_PRICE = 10_000


@dataclass(frozen=True)
class Kline:
    """个股 K 线，见 DataService.get_kline。"""

    #: (date, open, high, low, close, open_raw, high_raw, low_raw, close_raw, amount, pct_chg)，按日期排序
    rows: pl.DataFrame
    #: 复权基准日：这只股票本地最后一个交易日，这一天的前复权价就是真实价格。区间之后都没有行情时为空
    base_date: date | None


@dataclass(frozen=True)
class BoardInfo:
    code: str
    name: str
    board_type: str  # sw_industry / concept


def _previous_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:])
    return f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"


def _trim_before(rows: pl.DataFrame, start: date, lookback: int) -> pl.DataFrame:
    """留下 start 起的行，再给这些标的各留 start 之前自己最近的 lookback 条。"""
    in_range = rows.filter(pl.col("date") >= start)
    if lookback == 0:
        return in_range
    earlier = (
        rows.filter(pl.col("date") < start)
        .join(in_range.select("code").unique(), on="code", how="semi")
        .filter(pl.col("date").rank("ordinal", descending=True).over("code") <= lookback)
    )
    return pl.concat([earlier, in_range])


class DataService:
    """读本地数据。只有这一个实现，不做抽象接口（§1.2）。"""

    def __init__(self, store: MarketStore):
        self._store = store

    @classmethod
    def from_env(cls) -> DataService:
        return cls(MarketStore.from_env())

    def available_targets(self) -> tuple[str, ...]:
        """当前能用的标的类型：股票、申万行业总是有，概念板块要能力探测通过。

        能力只针对整类标的，所以表达式校验可以保持纯函数，调用方先用它判断标的能不能用（§3.4）。
        """
        return available_targets(Manifest.load(self._store))

    # ── 股票信息与指数 ──────────────────────────────────────────

    def stock_info(self, codes: Collection[str], as_of: date) -> pl.DataFrame:
        """股票在 as_of 那天的名称、申万一级行业，以及上市日、退市日。

        返回 (code, name, industry, list_date, delist_date)，按代码排序，每个代码一行；查不到的字段为空值，不报错。

        - **名称取当天的**：曾用名表里 as_of 之后还有改名记录，说明 as_of 那天的名字已经收录，
          就用当天生效的那条（开始日期最大的一条，§2.6）；否则用股票列表里的现用名。
          曾用名表有滞后——2026-09-14 实测 688189.SH 已改名「ST南新」、000595.SZ 已改名「新能股份」，
          曾用名表都还没收录——所以最近一次改名还没收录时，显示的是现用名
        - 行业按当天的归属，规则同按行业选股（universe.industry_of）
        """
        chosen = sorted(set(codes))
        frame = pl.DataFrame({"code": chosen}, schema={"code": pl.String})
        basic = (
            self._scan_table(STOCK_BASIC_TABLE)
            .select("code", "name", "list_date", "delist_date")
            .collect()
        )
        renames = (
            self._scan_table(NAMECHANGE_TABLE)
            .filter(pl.col("code").is_in(chosen))
            .select("code", "name", "start_date")
            .collect()
            .unique()
        )
        renamed_later = renames.filter(pl.col("start_date") > as_of).select("code").unique()
        name_then = (
            renames.filter(pl.col("start_date") <= as_of)
            .join(renamed_later, on="code", how="semi")
            .sort("code", "start_date", "name")
            .group_by("code", maintain_order=True)
            .agg(pl.col("name").last().alias("_name_then"))
        )
        sw_member = self._scan_table(SW_MEMBER_TABLE).filter(pl.col("code").is_in(chosen)).collect()
        industry_names = (
            self._scan_table(SW_INDUSTRY_TABLE)
            .select(pl.col("code").alias("industry_code"), pl.col("name").alias("industry"))
            .collect()
        )
        belongs = (
            industry_of(frame.with_columns(pl.lit(as_of).alias("date")), sw_member)
            .join(industry_names, on="industry_code", how="left")
            .select("code", "industry")
        )
        return (
            frame.join(basic, on="code", how="left")
            .join(name_then, on="code", how="left")
            .with_columns(pl.coalesce("_name_then", "name").alias("name"))
            .join(belongs, on="code", how="left")
            .select("code", "name", "industry", "list_date", "delist_date")
            .sort("code")
        )

    def get_index_daily(self, code: str, start: date, end: date) -> pl.DataFrame:
        """宽基指数日线：(date, code, open, close)，按日期排序。个股回看把对照换成指数时用。

        本地只同步了沪深300（000300.SH）和中证500（000905.SH）。
        """
        if code not in BENCHMARK_INDEXES:
            raise ValueError(f"本地没有指数 {code} 的日线，可选 {list(BENCHMARK_INDEXES)}")
        if start > end:
            raise ValueError(f"起始日 {start} 晚于结束日 {end}")
        return (
            self._scan_table(INDEX_DAILY_TABLE)
            .filter((pl.col("code") == code) & pl.col("date").is_between(start, end))
            .select("date", "code", "open", "close")
            .sort("date")
            .collect()
        )

    # ── 覆盖范围与交易日历 ──────────────────────────────────────

    def latest_trading_day(self) -> date:
        """本地最后一个完整交易日，和页面上「数据截至」是同一天。

        磁盘上不存在半天的数据：一个交易日的必需接口全部拉到才落盘（§2.5），所以落盘的最后一天就是完整日。
        """
        return self.data_range(STOCK)[1]

    def data_range(self, target: str = STOCK) -> tuple[date, date]:
        """某类标的本地数据的起止日。取数区间必须落在里面：调用方先用它把区间裁好，比如预热期不能早于起点。

        股票取从最新一天往回**连续**覆盖的区间（DataSync.status 的「历史已补到」~「数据截至」），
        中间有缺口的月份之前都不算；板块取板块日线的首末日（申万 2016 年起，概念板块 2025-03-28 起）。
        """
        manifest = Manifest.load(self._store)
        self._check_target(target, manifest)
        if target == STOCK:
            status = DataSync(None, self._store).status(manifest)
            if status.history_from is None or status.data_through is None:
                raise MissingDataError("本地还没有股票日频数据，先同步")
            return date.fromisoformat(status.history_from), date.fromisoformat(status.data_through)
        first, last = (
            self._scan_table(_BOARD_DAILY[target])
            .select(pl.col("date").min(), pl.col("date").max().alias("last"))
            .collect()
            .row(0)
        )
        return first, last

    def get_trading_calendar(self, start: date, end: date) -> list[date]:
        """[start, end] 里的交易日，从早到晚。只含本地交易日历覆盖到的日子（同步时从 2016 年拉到同步当天）。"""
        days = (
            self._scan_table(TRADE_CAL_TABLE)
            .filter(pl.col("is_open") & pl.col("date").is_between(start, end))
            .select("date")
            .collect()
        )
        return sorted(days.get_column("date").to_list())

    # ── 取数 ────────────────────────────────────────────────────

    def get_fields(
        self,
        codes: Collection[str] | None,
        start: date,
        end: date,
        fields: Sequence[str],
        target: str = STOCK,
        lookback: int = 0,
    ) -> pl.DataFrame:
        """统一取数入口：长表 (date, code, <fields…>)，按 (date, code) 排序，字段列按 fields 的顺序。

        - codes 为 None 表示这类标的的全部
        - 股票价格一律后复权；财务按公告日对齐；事件、状态为布尔值。涨跌停标记只在数据源缺了涨跌停价的日子
          为空值（2016~2019 年 886 行，判断不了、不猜）；不设涨跌幅限制的日子（上市首日、退市整理期等）都是 False
        - lookback：每只标的再往前带上**它自己的**最多 lookback 条行情（停牌日不算，不够就有多少带多少）。
          这些行早于 start，给表达式引擎预热用，算完由调用方截掉。按交易日历往前推会让停过牌的股票凑不够（§3.6）
        - 停牌日没有行（面板不补齐，§2.2）
        - 股票可以取 research 用的内部列（INTERNAL_COLUMNS）。表达式碰不到它们，由 expr 的校验器按 FIELDS 拦
        - 字段不在 FIELDS 里、不属于这类标的、这类标的当前不可用、区间超出本地数据，一律报错，不返回空列
        """
        manifest = Manifest.load(self._store)
        self._check_target(target, manifest)
        wanted = list(dict.fromkeys(fields))
        if not wanted:
            raise ValueError("fields 不能为空")
        for name in wanted:
            if not (target == STOCK and name in INTERNAL_COLUMNS):
                check_available([name], target)
        if lookback < 0:
            raise ValueError(f"lookback 不能是负数，收到 {lookback}")
        self._check_range(start, end, target)
        chosen = None if codes is None else sorted(set(codes))

        if target == STOCK:
            table = self._stock_fields(chosen, start, end, wanted, lookback)
        else:
            # 板块日线整张才十万行上下，直接读到 end 再截
            query = self._scan_table(_BOARD_DAILY[target]).filter(pl.col("date") <= end)
            if chosen is not None:
                query = query.filter(pl.col("code").is_in(chosen))
            table = _trim_before(query.select("date", "code", *wanted).collect(), start, lookback)
        return table.select("date", "code", *wanted).sort("date", "code")

    def get_kline(self, code: str, start: date, end: date) -> Kline:
        """个股 K 线：前复权的开高低收、当天的真实成交价、成交额、涨跌幅（2026-09-15 定）。

        - **前复权** = 后复权价 ÷ 这只股票本地最后一个交易日的复权因子。最后一天的价格就是真实价格，
          和行情软件默认的显示方式一致；整段价格只差一个固定倍数，任何一段的涨跌幅都和后复权一样——
          个股回看的收益是用后复权算的，图和表对得上。同步了新数据、期间又除权的话，整张图的价格会整体变一点
        - **真实成交价** = 后复权价 ÷ 当天的复权因子（2026-09-14 实测和不复权收盘价的误差在 1e-13 以内）
        - 不给成交量：它随复权调整过（原始股数 ÷ 复权因子），画量柱用成交额
        - 区间超出本地数据报错；区间里没有行情（还没上市、已退市、长期停牌）返回空表
        """
        self._check_range(start, end, STOCK)
        latest = self.data_range(STOCK)[1]
        prices = ("open", "high", "low", "close")
        columns = ["date", *prices, "adj_factor", "amount", "pct_chg"]
        rows = self._read_panel(start, latest, [code], columns).sort("date")
        base_date, base_factor = (
            (None, 1.0) if rows.is_empty() else rows.select("date", "adj_factor").row(-1)
        )
        table = rows.filter(pl.col("date") <= end).select(
            "date",
            *[(pl.col(name) / base_factor).alias(name) for name in prices],
            *[(pl.col(name) / pl.col("adj_factor")).alias(f"{name}_raw") for name in prices],
            "amount",
            "pct_chg",
        )
        return Kline(table, base_date)

    def _stock_fields(
        self, codes: list[str] | None, start: date, end: date, fields: list[str], lookback: int
    ) -> pl.DataFrame:
        wanted = set(fields)
        limit_flags = sorted(wanted & _LIMIT_FLAGS)
        helpers = (["adj_factor"] if "is_ex_div" in wanted else []) + (
            ["up_limit"] if limit_flags else []
        )
        stored = [name for name in fields if name not in DERIVED_FIELDS]
        columns = list(dict.fromkeys(["date", "code", *stored, *helpers]))
        rows = self._read_panel(start, end, codes, columns)

        coverage_start = self.data_range(STOCK)[0]
        list_dates = self._scan_table(STOCK_BASIC_TABLE).select("code", "list_date").collect()
        # 往前带的条数：预热要 lookback 条；除权、公告顺延还要拿最早那条和它的前一条比，再多带一条，最后截掉
        count = lookback + (1 if wanted & _NEEDS_PREVIOUS_ROW else 0)
        if count:
            earlier = self._rows_before(
                start, rows.get_column("code").unique(), columns, count, list_dates, coverage_start
            )
            rows = pl.concat([*earlier, rows])

        if limit_flags:
            no_limit = pl.col("up_limit") >= NO_LIMIT_PRICE
            rows = rows.with_columns(
                pl.when(no_limit).then(False).otherwise(pl.col(flag)).alias(flag)
                for flag in limit_flags
            )
        if "is_ex_div" in wanted:
            rows = with_ex_div(rows)
        for name, (table, column) in _EVENT_SOURCES.items():
            if name in wanted:
                events = self._scan_table(table).select("code", pl.col(column).alias("date"))
                rows = with_event(
                    rows,
                    events.collect(),
                    name,
                    list_dates=list_dates,
                    since=coverage_start.replace(day=1),
                )
        if "is_new" in wanted:
            rows = with_is_new(rows, list_dates, self.get_trading_calendar(date.min, date.max))
        if wanted & FINANCE_FIELDS.keys():
            fina = self._scan_table(FINA_INDICATOR_TABLE)
            if codes is not None:
                fina = fina.filter(pl.col("code").is_in(codes))
            rows = with_finance(rows, finance_timeline(fina.collect()))
        return _trim_before(rows, start, lookback)

    def _read_panel(
        self, start: date, end: date, codes: list[str] | None, columns: Sequence[str]
    ) -> pl.DataFrame:
        months = months_between(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        missing = [month for month in months if not self._store.has_month(DAILY_DATASET, month)]
        if missing:
            raise MissingDataError(f"本地股票日频缺 {missing}")
        query = pl.scan_parquet(
            [self._store.month_path(DAILY_DATASET, month) for month in months]
        ).filter(pl.col("date").is_between(start, end))
        if codes is not None:
            query = query.filter(pl.col("code").is_in(codes))
        return query.select(columns).collect()

    def _rows_before(
        self,
        start: date,
        codes: pl.Series,
        columns: Sequence[str],
        count: int,
        list_dates: pl.DataFrame,
        coverage_start: date,
    ) -> list[pl.DataFrame]:
        """每只股票在 start 之前自己最近的 count 条行情，不够就有多少取多少。

        先按交易日历往前推 count 天，一次读完这几个月——没停过牌的股票这一下就够了；停过牌还不够的，
        再一个月一个月往前补，直到够数或翻到本地数据起点（实测停牌超过一年才复牌的有 183 次）。
        上市日不早于 start 的股票之前本来就没有行，直接跳过，否则每只新股都要翻到最早的月份。
        """
        listed_later = set(list_dates.filter(pl.col("list_date") >= start).get_column("code"))
        remaining = {code: count for code in codes.to_list() if code not in listed_later}
        before = [day for day in self.get_trading_calendar(coverage_start, start) if day < start]
        if not remaining or not before:
            return []
        first_month = coverage_start.strftime("%Y-%m")
        earliest = before[-count] if len(before) >= count else before[0]
        batch = [
            month
            for month in months_between(earliest.strftime("%Y%m%d"), start.strftime("%Y%m%d"))
            if self._store.has_month(DAILY_DATASET, month)
        ]
        month = earliest.strftime("%Y-%m")

        found: list[pl.DataFrame] = []
        while remaining and batch:
            part = (
                pl.scan_parquet([self._store.month_path(DAILY_DATASET, m) for m in batch])
                .filter((pl.col("date") < start) & pl.col("code").is_in(sorted(remaining)))
                .select(columns)
                .collect()
            )
            found.append(part)
            for code, rows in part.group_by("code").len().iter_rows():
                remaining[code] -= rows
            remaining = {code: left for code, left in remaining.items() if left > 0}
            batch = []
            while remaining and not batch:
                month = _previous_month(month)
                if month < first_month:
                    break
                if self._store.has_month(DAILY_DATASET, month):
                    batch = [month]
        if not found:
            return []
        earlier = pl.concat(found)
        newest_first = pl.col("date").rank("ordinal", descending=True).over("code")
        return [earlier.filter(newest_first <= count)]

    # ── 股票池 ──────────────────────────────────────────────────

    def get_universe_mask(
        self,
        start: date,
        end: date,
        base: str = "all_a",
        industry: str | None = None,
        board: Mapping[str, str] | None = None,
        exclude: Sequence[str] | None = None,
    ) -> pl.DataFrame:
        """每天的股票池：在池内的 (date, code)，按 (date, code) 排序，不在池内的不出现。

        历史计算必须用它，不能拿某一天的名单去套整段历史——那是幸存者偏差。规则见 universe.py。

        - base：all_a（沪深 A 股，不含北交所）/ hs300 / zz500
        - industry：申万一级行业名，按每天当时的归属
        - board：`{"type": "concept", "code": "880728.TDX"}`，P0 用快照日的当前成分
        - exclude：ST / suspended / new_listing_<N>d
        """
        if base not in BASES:
            raise ValueError(f"不认识的股票池 {base!r}，可选 {list(BASES)}")
        self._check_range(start, end, STOCK)
        traded = self._read_panel(start, end, None, ["date", "code", "is_st"])
        return universe_mask(
            traded,
            base=base,
            exclude=exclude or (),
            industry_code=None if industry is None else self._industry_code(industry),
            board_codes=None if board is None else self._concept_members(board),
            list_dates=self._scan_table(STOCK_BASIC_TABLE).select("code", "list_date").collect(),
            calendar=self.get_trading_calendar(date.min, date.max),
            weights=None if BASES[base] is None else self._index_weights(end),
            sw_member=None if industry is None else self._scan_table(SW_MEMBER_TABLE).collect(),
        )

    def get_universe(
        self,
        as_of: date,
        base: str = "all_a",
        industry: str | None = None,
        board: Mapping[str, str] | None = None,
        exclude: Sequence[str] | None = None,
    ) -> list[str]:
        """某一天的股票池，代码从小到大。as_of 必须是本地数据覆盖到的交易日——非交易日报错，不给空池子。"""
        self._check_range(as_of, as_of, STOCK)
        if not self.get_trading_calendar(as_of, as_of):
            raise ValueError(f"{as_of} 不是交易日")
        mask = self.get_universe_mask(as_of, as_of, base, industry, board, exclude)
        return mask.get_column("code").to_list()

    def _index_weights(self, end: date) -> pl.DataFrame:
        months = [m for m in self._store.months(INDEX_WEIGHT_DATASET) if m <= end.strftime("%Y-%m")]
        if not months:
            raise MissingDataError("本地还没有指数成分数据，先同步")
        paths = [self._store.month_path(INDEX_WEIGHT_DATASET, month) for month in months]
        return pl.scan_parquet(paths).select("index_code", "code", "date").collect()

    def _industry_code(self, name: str) -> str:
        industries = self._scan_table(SW_INDUSTRY_TABLE).select("code", "name").collect()
        match = industries.filter(pl.col("name") == name)
        if match.is_empty():
            choices = "、".join(industries.get_column("name").to_list())
            raise ValueError(f"没有叫 {name!r} 的申万一级行业，可选：{choices}")
        return match.get_column("code")[0]

    def _concept_members(self, board: Mapping[str, str]) -> list[str]:
        if board.get("type") != CONCEPT:
            raise ValueError(
                f"board 只支持概念板块 {{'type': 'concept', 'code': ...}}，收到 {dict(board)}；"
                "申万行业用 industry"
            )
        return self.board_members(board["code"])

    # ── 板块 ────────────────────────────────────────────────────

    def list_boards(self, board_type: str) -> list[BoardInfo]:
        """板块清单，按代码排序。sw_industry：申万一级行业；concept：通达信概念板块，需要能力可用。"""
        if board_type == SW_INDUSTRY:
            table = SW_INDUSTRY_TABLE
        elif board_type == CONCEPT:
            self._check_target(CONCEPT, Manifest.load(self._store))
            table = TDX_CONCEPT_TABLE
        else:
            raise ValueError(f"不认识的板块类型 {board_type!r}，可选 sw_industry、concept")
        boards = self._scan_table(table).select("code", "name").sort("code").collect()
        return [BoardInfo(code, name, board_type) for code, name in boards.iter_rows()]

    def concept_snapshot_date(self) -> date:
        """概念板块清单与成分的快照日。P0 概念板块只有这一天的成分（§2.7），需要能力可用。"""
        self._check_target(CONCEPT, Manifest.load(self._store))
        return self._scan_table(TDX_CONCEPT_TABLE).select(pl.col("date").max()).collect().item()

    def board_members(self, board_code: str, as_of: date | None = None) -> list[str]:
        """板块的成分股代码，从小到大，不含北交所。

        申万行业按 as_of 当天的归属，默认本地最新一天。概念板块只有快照日的当前成分（§2.7），
        as_of 早于快照日直接报错——拿今天的成分回答过去，是幸存者偏差。
        """
        industries = self._scan_table(SW_INDUSTRY_TABLE).select("code").collect()
        if board_code in industries.get_column("code").to_list():
            day = as_of or self.latest_trading_day()
            sw_member = self._scan_table(SW_MEMBER_TABLE).collect()
            pool = (
                sw_member.filter(pl.col("industry_code") == board_code)
                .select("code")
                .unique()
                .with_columns(pl.lit(day).alias("date"))
            )
            codes = industry_members(pool, sw_member, board_code).get_column("code").to_list()
        else:
            snapshot = self.concept_snapshot_date()
            concepts = self._scan_table(TDX_CONCEPT_TABLE).select("code").collect()
            if board_code not in concepts.get_column("code").to_list():
                raise ValueError(f"不认识的板块代码 {board_code!r}")
            if as_of is not None and as_of < snapshot:
                raise MissingDataError(
                    f"概念板块只有 {snapshot} 的当前成分，查不了 {as_of} 当时的成分"
                )
            members = self._scan_table(TDX_MEMBER_TABLE).filter(pl.col("board_code") == board_code)
            codes = members.select("code").collect().get_column("code").to_list()
        return sorted({code for code in codes if not code.endswith(".BJ")})

    # ── 内部 ────────────────────────────────────────────────────

    def _scan_table(self, name: str) -> pl.LazyFrame:
        if not self._store.has_table(name):
            raise MissingDataError(f"本地还没有 {name}，先同步")
        return pl.scan_parquet(self._store.table_path(name))

    @staticmethod
    def _check_target(target: str, manifest: Manifest) -> None:
        if target not in (STOCK, *_BOARD_DAILY):
            raise ValueError(f"不认识的标的类型 {target!r}，可选 stock、sw_industry、concept")
        if target not in available_targets(manifest):
            reason = manifest.unavailable_reason(TARGET_CAPABILITIES[target])
            raise MissingDataError(f"{target} 当前不可用：{reason}")

    def _check_range(self, start: date, end: date, target: str) -> None:
        if start > end:
            raise ValueError(f"起始日 {start} 晚于结束日 {end}")
        first, last = self.data_range(target)
        if start < first or end > last:
            raise MissingDataError(
                f"请求 {start} ~ {end}，本地 {target} 数据只覆盖 {first} ~ {last}"
            )
