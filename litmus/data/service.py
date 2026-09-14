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
    DAILY_DATASET,
    DISCLOSURE_TABLE,
    FINA_INDICATOR_TABLE,
    FORECAST_TABLE,
    INDEX_WEIGHT_DATASET,
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
from litmus.data.universe import BASES, industry_members, universe_mask

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


@dataclass(frozen=True)
class BoardInfo:
    code: str
    name: str
    board_type: str  # sw_industry / concept


def _previous_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:])
    return f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"


class DataService:
    """读本地数据。只有这一个实现，不做抽象接口（§1.2）。"""

    def __init__(self, store: MarketStore):
        self._store = store

    @classmethod
    def from_env(cls) -> DataService:
        return cls(MarketStore.from_env())

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
    ) -> pl.DataFrame:
        """统一取数入口：长表 (date, code, <fields…>)，按 (date, code) 排序，字段列按 fields 的顺序。

        - codes 为 None 表示这类标的的全部
        - 股票价格一律后复权；财务按公告日对齐；事件、状态为布尔值，没有空值
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
        self._check_range(start, end, target)
        chosen = None if codes is None else sorted(set(codes))

        if target == STOCK:
            table = self._stock_fields(chosen, start, end, wanted)
        else:
            query = self._scan_table(_BOARD_DAILY[target]).filter(
                pl.col("date").is_between(start, end)
            )
            if chosen is not None:
                query = query.filter(pl.col("code").is_in(chosen))
            table = query.select("date", "code", *wanted).collect()
        return table.select("date", "code", *wanted).sort("date", "code")

    def _stock_fields(
        self, codes: list[str] | None, start: date, end: date, fields: list[str]
    ) -> pl.DataFrame:
        wanted = set(fields)
        columns = ["date", "code", *(name for name in fields if name not in DERIVED_FIELDS)]
        if "is_ex_div" in wanted and "adj_factor" not in columns:
            columns.append("adj_factor")
        rows = self._read_panel(start, end, codes, columns)

        coverage_start = self.data_range(STOCK)[0]
        list_dates = self._scan_table(STOCK_BASIC_TABLE).select("code", "list_date").collect()
        if wanted & _NEEDS_PREVIOUS_ROW:
            # 起点那天的除权、起点之前停牌期间发的公告，都要和每只股票起点之前的最后一行比；
            # 这些行只用来算，最后截掉
            earlier = self._last_rows_before(
                start, rows.get_column("code").unique(), columns, list_dates, coverage_start
            )
            rows = pl.concat([*earlier, rows])

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
        return rows.filter(pl.col("date") >= start)

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

    def _last_rows_before(
        self,
        start: date,
        codes: pl.Series,
        columns: Sequence[str],
        list_dates: pl.DataFrame,
        coverage_start: date,
    ) -> list[pl.DataFrame]:
        """每只股票在 start 之前的最后一行。从 start 所在的月往回一个月一个月地找，找齐或翻到本地数据起点为止。

        上市日不早于 start 的股票之前本来就没有行，直接跳过，否则每只新股都要翻到最早的月份。
        停牌很久的要多翻几个月（实测停牌超过一年才复牌的有 183 次），每个月只读这几列、只筛还没找到的股票。
        """
        listed_later = list_dates.filter(pl.col("list_date") >= start).get_column("code")
        pending = set(codes.to_list()) - set(listed_later.to_list())
        first_month = coverage_start.strftime("%Y-%m")
        month = start.strftime("%Y-%m")
        found: list[pl.DataFrame] = []
        while pending and month >= first_month:
            if self._store.has_month(DAILY_DATASET, month):
                part = (
                    pl.scan_parquet(self._store.month_path(DAILY_DATASET, month))
                    .filter((pl.col("date") < start) & pl.col("code").is_in(sorted(pending)))
                    .select(columns)
                    .collect()
                )
                last = part.filter(pl.col("date") == pl.col("date").max().over("code"))
                found.append(last)
                pending -= set(last.get_column("code").to_list())
            month = _previous_month(month)
        return found

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
            self._check_target(CONCEPT, Manifest.load(self._store))
            concepts = self._scan_table(TDX_CONCEPT_TABLE).select("code", "date").collect()
            if board_code not in concepts.get_column("code").to_list():
                raise ValueError(f"不认识的板块代码 {board_code!r}")
            snapshot = concepts.get_column("date").max()
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
