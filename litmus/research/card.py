"""卡：点名看的几个标的，一个指标一行，下面跟一行解释（DESIGN.md §1.5）。

不筛不排。指标由 compute 算好，这里补上解释要用的数，交给 `spec.card` 拼成话：

- 指标按公式最外层分类：分位 `TsRank`、涨跌 `Pct` / `PctSince`、排名 `Rank`、条件（真假）、其余的数
- `TsRank(X, n)` 里的 X 也是卡上的一个指标时，分位不单独占一行，写进 X 的解释行
- 排名写成「第几名（共几只）」：在整个算的范围里数有几个比它高，并列的写「并列第几」
- 涨跌给同期申万一级行业指数做对照
- 值为空时查清为什么：还没上市、停牌、被算的范围剔除、亏损没有市盈率、行情不够长、窗口里有空值
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import polars as pl

from litmus.data import FIELDS, STOCK, SW_INDUSTRY, DataService, MissingDataError
from litmus.expr import (
    COMPARISONS,
    RATIO,
    Binary,
    Call,
    ExprDataError,
    Field,
    Node,
    Number,
    Unary,
    anchor_date,
    collect_fields,
    collect_lookback,
    describe,
    evaluate,
    parse,
    result_unit,
    validate,
)
from litmus.research.compute import Computed, board_notes, compute
from litmus.research.results import CardItem, CardResult, CardRow
from litmus.spec import MetricFacts, Missing, QuerySpec, Report, card, render_row

#: 后复权的价格字段：卡上要提醒它不是当天的成交价
_ADJUSTED = frozenset({"open", "high", "low", "close", "vwap"})
#: 财务字段：为空多半是还没公告过财报
_FINANCE = frozenset({"roe", "revenue_yoy", "profit_yoy"})
#: 结果是条件（真假）的算子
_CONDITIONS = frozenset({"Cross"})

#: 宽基指数的叫法
_INDEX_LABELS = {"000300.SH": "沪深300 指数", "000905.SH": "中证500 指数"}

#: 还没查过。查出来是 None（没有行情、没有财报）也要记住，别每次重查
_UNSET = object()


def run_card(spec: QuerySpec, ds: DataService) -> CardResult:
    computed = compute(spec, ds)
    nodes = {metric.name: parse(metric.expr) for metric in spec.metrics}
    folded = _folded(spec, nodes)
    items = []
    for code in spec.subject.codes:
        subject = _Subject(code, spec, computed, ds)
        facts = {name: subject.facts(name, node) for name, node in nodes.items()}
        rows = []
        for metric in spec.metrics:
            if metric.name in folded:
                continue
            fact = facts[metric.name]
            merged = [facts[name] for name, base in folded.items() if base == metric.name]
            if merged:
                fact = replace(fact, percentile=merged[0])
            text, note = render_row(fact)
            rows.append(CardRow(metric.name, _plain(fact.value), fact.unit, text, note))
        items.append(CardItem(code, subject.name, subject.industry, tuple(rows)))
    return CardResult(
        as_of=computed.day,
        pool_size=computed.pool_size,
        items=tuple(items),
        notes=board_notes(spec, ds),
    )


def _folded(spec: QuerySpec, nodes: dict[str, Node]) -> dict[str, str]:
    """分位指标 → 被它算分位的那个指标。两个都在卡上时分位不单独占一行。"""
    keys = {name: _key(node) for name, node in nodes.items()}
    folded = {}
    taken: set[str] = set()
    for metric in spec.metrics:
        node = nodes[metric.name]
        if not (isinstance(node, Call) and node.name == "TsRank"):
            continue
        inner = _key(node.args[0])
        base = next(
            (name for name, key in keys.items() if key == inner and name not in taken), None
        )
        if base is not None:
            folded[metric.name] = base
            taken.add(base)
    return folded


def _key(node: Node) -> tuple:
    """语法树的比较用键：`TsRank($pb, 500)` 里的 `$pb` 和另一个指标的 `$pb` 是同一个数。"""
    if isinstance(node, Number):
        return ("n", node.value)
    if isinstance(node, Field):
        return ("f", node.name)
    if isinstance(node, Unary):
        return ("u", node.op, _key(node.operand))
    if isinstance(node, Binary):
        return ("b", node.op, _key(node.left), _key(node.right))
    return ("c", node.name, tuple(_key(arg) for arg in node.args))


def _plain(value: object) -> float | bool | None:
    return value if isinstance(value, bool | float | int) else None  # type: ignore[return-value]


class _Subject:
    """卡上的一个标的：算它的每个指标，查它空值的原因。"""

    def __init__(self, code: str, spec: QuerySpec, computed: Computed, ds: DataService):
        self._code = code
        self._spec = spec
        self._computed = computed
        self._ds = ds
        self._day = computed.day
        self._target = spec.scope.target
        self._row = _row_of(computed.table, code)
        self._info_cache: dict | None = None
        self._quote_cache: object = _UNSET
        self._report_cache: object = _UNSET
        self._pool_cache: set[str] | None = None
        self.name, self.industry = self._who()

    # ── 一个指标 ────────────────────────────────────────────────

    def facts(self, name: str, node: Node) -> MetricFacts:
        return self._facts(node, self._row.get(name), ranked=self._computed.ranked.get(name))

    def _facts(self, node: Node, value: object, ranked: pl.DataFrame | None = None) -> MetricFacts:
        if isinstance(node, Call) and node.name == "TsRank":
            return self._percentile(node, value)
        if isinstance(node, Call) and node.name in ("Pct", "PctSince") or _is_daily(node):
            return self._change(node, value)
        if isinstance(node, Call) and node.name == "Rank" and ranked is not None:
            return self._rank(node, value, ranked)
        if _is_condition(node):
            return self._condition(node, value)
        return self._value(node, value)

    def _value(self, node: Node, value: object) -> MetricFacts:
        field = node.name if isinstance(node, Field) else None
        adjusted = field in _ADJUSTED and self._target == STOCK
        return MetricFacts(
            kind=card.VALUE,
            value=_plain(value),
            unit=result_unit(node),
            field=field,
            label=describe(node, self._target),
            missing=None if value is not None else self._why(node),
            adjusted=adjusted,
            raw_close=self._today(("close_raw",)).get("close_raw") if field == "close" else None,
            report=self._report if field in _FINANCE and value is not None else None,
        )

    def _percentile(self, node: Call, value: object) -> MetricFacts:
        inner, window = node.args[0], int(_window(node))
        current = self._facts(inner, self._eval(inner))
        seen = self._window_values(inner, window) if value is not None else pl.Series([])
        return MetricFacts(
            kind=card.PERCENTILE,
            value=_plain(value),
            unit=RATIO,
            label=describe(node, self._target),
            missing=None if value is not None else (current.missing or self._why(node)),
            window=window,
            low=_plain(seen.min()) if seen.len() else None,
            high=_plain(seen.max()) if seen.len() else None,
            counted=seen.len(),
            inner=current,
        )

    def _change(self, node: Node, value: object) -> MetricFacts:
        daily = _is_daily(node)
        return MetricFacts(
            kind=card.CHANGE,
            value=_plain(value),
            unit="%" if daily else RATIO,
            field="pct_chg" if daily else None,
            label=describe(node, self._target),
            missing=None if value is not None else self._why(node),
            period=self._period(node),
            benchmark=self._benchmark(node),
        )

    def _rank(self, node: Call, value: object, ranked: pl.DataFrame) -> MetricFacts:
        inner = node.args[0]
        column = ranked.get_column("value") if ranked.height else pl.Series("value", [], pl.Float64)
        total = int(column.len() - column.null_count())
        position = tied = None
        if value is not None:
            higher = int((column > float(value)).sum())  # type: ignore[arg-type]
            position = higher + 1
            tied = int((column == float(value)).sum()) > 1  # type: ignore[arg-type]
        return MetricFacts(
            kind=card.RANK,
            value=_plain(value),
            unit=RATIO,
            label=describe(node, self._target),
            missing=None if value is not None else self._not_ranked(inner),
            position=position,
            total=total,
            tied=bool(tied),
            measure="只" if self._target == STOCK else "个",
            inner=self._facts(inner, self._eval(inner)),
        )

    def _condition(self, node: Node, value: object) -> MetricFacts:
        sides: tuple[MetricFacts, ...] = ()
        relative = False
        if isinstance(node, Binary) and node.op in COMPARISONS:
            parts = [side for side in (node.left, node.right) if not isinstance(side, Number)]
            sides = tuple(self._facts(side, self._eval(side)) for side in parts)
            relative = len(parts) == 2 and all(
                collect_fields(side) & _ADJUSTED
                for side in parts  # type: ignore[arg-type]
            )
        return MetricFacts(
            kind=card.CONDITION,
            value=_plain(value),
            unit=result_unit(node),
            label=describe(node, self._target),
            missing=None if value is not None else self._why(node),
            sides=sides,
            relative=relative,
        )

    # ── 解释要用的数 ────────────────────────────────────────────

    def _eval(self, node: Node) -> float | bool | None:
        """这个标的当天的一个数。算不出来（行情不够）就是空。"""
        rows = pl.DataFrame(
            {"date": [self._day], "code": [self._code]}, schema={"date": pl.Date, "code": pl.String}
        )
        try:
            values = evaluate(
                node, "metric", rows, self._day, self._day, self._ds, target=self._target
            ).values
        except ExprDataError:
            return None
        return None if values.is_empty() else values.get_column("value").item()

    def _window_values(self, node: Node, window: int) -> pl.Series:
        """这个数在最近 window 个交易日（这个标的自己的）里有值的那些天，和分位同一个口径。"""
        days = self._ds.get_fields(
            [self._code], self._day, self._day, ["close"], self._target, lookback=window - 1
        ).select("date", "code")
        if days.is_empty():
            return pl.Series([], dtype=pl.Float64)
        first = days.get_column("date").min()
        try:
            values = evaluate(
                node, "metric", days, first, self._day, self._ds, target=self._target
            ).values
        except ExprDataError:
            return pl.Series([], dtype=pl.Float64)
        return values.get_column("value").drop_nulls()

    def _period(self, node: Node) -> str:
        """涨跌算的是哪一段：「当日」「今年以来」「近 20 个交易日」「2026-03-31 以来」。"""
        if _is_daily(node):
            return "当日"
        assert isinstance(node, Call)
        subject = node.args[0]
        prefix = "" if _key(subject) == ("f", "close") else describe(subject, self._target)
        if node.name == "Pct":
            return f"{prefix}近 {int(_window(node))} 个交易日"
        anchor = anchor_date(node.args[1])
        if anchor is None:
            return prefix
        if self._since_new_year(anchor):
            return f"{prefix}今年以来"
        return f"{prefix}{anchor} 以来"

    def _since_new_year(self, anchor: date) -> bool:
        """起点是去年最后一个交易日：这一段就是「今年以来」。"""
        if anchor.year != self._day.year - 1:
            return False
        rest = self._ds.get_trading_calendar(anchor + timedelta(days=1), date(anchor.year, 12, 31))
        return not rest

    def _benchmark(self, node: Node) -> tuple[str, float | None] | None:
        """同期对照：默认是这只股票所属申万一级行业的指数，同一个公式算一遍；也可以换成宽基指数。
        对照一律是小数（-0.0304）：当日涨跌幅字段本身是百分数，算出来除以 100。"""
        benchmark = getattr(self._spec.output, "benchmark", "industry")
        if benchmark.startswith("index:"):
            code = benchmark.removeprefix("index:")
            return (_INDEX_LABELS[code], self._index_change(node, code))
        industry = self._industry_name
        if self._target != STOCK or not industry:
            return None
        if not validate(node, SW_INDUSTRY, "metric").ok:
            return None  # 这个公式用到的字段行业指数上没有
        code = next((b.code for b in self._ds.list_boards(SW_INDUSTRY) if b.name == industry), None)
        if code is None:
            return None
        rows = pl.DataFrame(
            {"date": [self._day], "code": [code]}, schema={"date": pl.Date, "code": pl.String}
        )
        try:
            values = evaluate(
                node, "metric", rows, self._day, self._day, self._ds, target=SW_INDUSTRY
            ).values
        except (ExprDataError, MissingDataError):
            return None
        value = None if values.is_empty() else values.get_column("value").item()
        if value is not None and _is_daily(node):
            value = value / 100
        return (f"{industry}行业指数", _plain(value))

    def _index_change(self, node: Node, code: str) -> float | None:
        """指数同一段的涨跌。只对收盘价的涨跌算：当日涨跌幅、Pct($close, n)、PctSince($close, 日期)。"""
        if _is_daily(node):
            node = Call("Pct", (Field("close", 0), Number(1.0, "1", 0)), 0)
        if not isinstance(node, Call) or _key(node.args[0]) != ("f", "close"):
            return None
        if node.name == "Pct":
            n = int(_window(node))
            days = self._ds.get_trading_calendar(self._day - timedelta(days=n * 2 + 30), self._day)
            if len(days) <= n:
                return None
            start = days[-1 - n]
        else:
            anchor = anchor_date(node.args[1])
            if anchor is None:
                return None
            start = anchor
        try:
            rows = self._ds.get_index_daily(code, start - timedelta(days=15), self._day)
        except (MissingDataError, ValueError):
            return None
        before = rows.filter(pl.col("date") <= start)
        today = rows.filter(pl.col("date") == self._day)
        if before.is_empty() or today.is_empty():
            return None
        return today.get_column("close").item() / before.get_column("close")[-1] - 1

    # ── 为什么是空 ──────────────────────────────────────────────

    def _why(self, node: Node) -> Missing:
        if status := self._status():
            return status
        fields = [name for name in FIELDS if name in collect_fields(node)]
        today = self._today(tuple(fields))
        for name in fields:
            if today.get(name) is None:
                return self._no_value(name)
        if missing := self._no_start(node):
            return missing
        lookback = collect_lookback(node)
        if lookback:
            history = self._history(tuple(fields), lookback)
            if history.height < lookback + 1:
                return Missing(card.SHORT, need=lookback + 1, have=history.height)
            for name in fields:
                if nulls := int(history.get_column(name).null_count()):
                    label = FIELDS[name].label
                    return Missing(card.GAPS, label=label, need=lookback + 1, have=nulls)
        return Missing(card.NOT_FINITE)

    def _not_ranked(self, inner: Node) -> Missing:
        """排名为空：先看标的本身，再看它在不在算的范围里，最后看被排的那个数。"""
        if status := self._status():
            return status
        if self._code not in self._pool_codes:
            return Missing(card.EXCLUDED, excluded=self._excluded())
        return self._why(inner)

    def _status(self) -> Missing | None:
        """还没上市、已经退市、当天停牌。"""
        if self._target != STOCK:
            return None if self._traded else Missing(card.NO_QUOTE)
        info = self._info
        listed, delisted = info.get("list_date"), info.get("delist_date")
        if listed is None or listed > self._day:
            return Missing(card.NOT_LISTED, day=listed)
        if delisted is not None and delisted <= self._day:
            return Missing(card.DELISTED, day=delisted)
        if not self._traded:
            return Missing(card.SUSPENDED, day=self._last_traded)
        return None

    def _no_value(self, name: str) -> Missing:
        label = FIELDS[name].label
        if name == "pe_ttm":
            return Missing(card.LOSS, label=label, report=self._report)
        if name in _FINANCE and self._report is None:
            return Missing(card.NO_REPORT, label=label)
        return Missing(card.NO_VALUE, label=label)

    def _no_start(self, node: Node) -> Missing | None:
        """`PctSince` 的起点那天还没上市，就没有起点。"""
        listed = self._info.get("list_date") if self._target == STOCK else None
        for anchor in _anchors(node):
            if listed is not None and listed > anchor:
                return Missing(card.NO_START, day=anchor)
        return None

    def _excluded(self) -> str:
        """被算的范围剔除的原因，接在「当天」后面。"""
        scope = self._spec.scope
        if "ST" in scope.exclude and self._today(("is_st",)).get("is_st"):
            return "是 ST 股"
        for token in scope.exclude:
            if token.startswith("new_listing_"):
                days = int(token.removeprefix("new_listing_").removesuffix("d"))
                listed = self._info.get("list_date")
                if listed is not None and len(self._calendar(listed)) <= days:
                    return f"上市不满 {days} 个交易日"
        return "不在算的范围里"

    # ── 查本地数据 ──────────────────────────────────────────────

    def _who(self) -> tuple[str | None, str | None]:
        if self._target != STOCK:
            name = next(
                (b.name for b in self._ds.list_boards(self._target) if b.code == self._code), None
            )
            return name, None
        info = self._info
        levels = [part for part in (info.get("industry"), info.get("industry_l2")) if part]
        return info.get("name"), " / ".join(levels) or None

    @property
    def _info(self) -> dict:
        if self._info_cache is None:
            frame = self._ds.stock_info([self._code], self._day)
            self._info_cache = frame.row(0, named=True) if frame.height else {}
        return self._info_cache

    @property
    def _industry_name(self) -> str | None:
        return self._info.get("industry") if self._target == STOCK else None

    @property
    def _traded(self) -> bool:
        return self._quote is not None and self._quote == self._day

    @property
    def _last_traded(self) -> date | None:
        return self._quote

    @property
    def _quote(self) -> date | None:
        """当天及之前最后一个有行情的交易日。一条行情都没有时为空。"""
        if self._quote_cache is _UNSET:
            rows = self._ds.get_fields(
                [self._code], self._day, self._day, ["close"], self._target, lookback=1
            )
            self._quote_cache = None if rows.is_empty() else rows.get_column("date").max()
        return self._quote_cache  # type: ignore[return-value]

    @property
    def _report(self) -> Report | None:
        if self._report_cache is _UNSET:
            self._report_cache = self._latest_report()
        return self._report_cache  # type: ignore[return-value]

    def _latest_report(self) -> Report | None:
        if self._target != STOCK:
            return None
        rows = self._ds.latest_reports([self._code], self._day)
        if rows.is_empty():
            return None
        row = rows.row(0, named=True)
        return Report(
            period=row["period"],
            roe=row["roe"],
            profit_yoy=row["profit_yoy"],
            announced=row["ann_date"],
        )

    @property
    def _pool_codes(self) -> set[str]:
        if self._pool_cache is None:
            self._pool_cache = set(self._computed.pool.get_column("code").to_list())
        return self._pool_cache

    def _today(self, fields: tuple[str, ...]) -> dict:
        """这个标的当天的几个字段。当天没有行情时是空的。"""
        usable = [name for name in fields if FIELDS[name].available_for(self._target)]
        if not usable:
            return {}
        rows = self._ds.get_fields([self._code], self._day, self._day, usable, self._target)
        return rows.row(0, named=True) if rows.height else {}

    def _history(self, fields: tuple[str, ...], lookback: int) -> pl.DataFrame:
        usable = [name for name in fields if FIELDS[name].available_for(self._target)]
        return self._ds.get_fields(
            [self._code], self._day, self._day, usable, self._target, lookback=lookback
        )

    def _calendar(self, since: date) -> list[date]:
        return self._ds.get_trading_calendar(since, self._day)


def _row_of(table: pl.DataFrame, code: str) -> dict:
    rows = table.filter(pl.col("code") == code)
    return rows.row(0, named=True) if rows.height else {}


def _window(node: Call) -> float:
    window = node.args[1]
    assert isinstance(window, Number)
    return window.value


def _anchors(node: Node) -> list[date]:
    if isinstance(node, Call):
        found = [day for arg in node.args for day in _anchors(arg)]
        if node.name == "PctSince" and (anchor := anchor_date(node.args[1])) is not None:
            found.append(anchor)
        return found
    if isinstance(node, Unary):
        return _anchors(node.operand)
    if isinstance(node, Binary):
        return _anchors(node.left) + _anchors(node.right)
    return []


def _is_daily(node: Node) -> bool:
    """当日涨跌幅字段 $pct_chg：和 Pct($close, 1) 一样是一段涨跌，只是写成了百分数。"""
    return isinstance(node, Field) and node.name == "pct_chg"


def _is_condition(node: Node) -> bool:
    """算出来是真假：比较、且或非、上穿，以及本身就是布尔的字段。"""
    if isinstance(node, Binary):
        return node.op in COMPARISONS or node.op in ("&", "|")
    if isinstance(node, Unary):
        return node.op == "~"
    if isinstance(node, Call):
        return node.name in _CONDITIONS
    if isinstance(node, Field):
        definition = FIELDS.get(node.name)
        return definition is not None and definition.dtype == "bool"
    return False
