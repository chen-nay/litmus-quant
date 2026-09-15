"""确认卡上的说明文字：由查询条件按模板生成，不经过大模型（ARCHITECTURE §5.4）。

- 改了参数，说明跟着变：文字全部从 spec 现生成，不存在「文字和参数对不上」
- 用户原话里的说法（「昨天」「放量」）由大模型指出对应哪个栏目（Mention，只有词、不含数字），数字和定义一律从 spec 取
- 用了默认值的栏目标出来（spec.defaults_used，由 api 按请求里缺了哪些栏目填），确认卡显示「默认值，可修改」
- spec 不依赖其他模块：表达式的中文、股票名、成分快照日这些要解析表达式或读数据才知道的，由调用方查好放进 Facts
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from litmus.spec.query_spec import (
    BoardListSpec,
    Condition,
    Sort,
    StockHistorySpec,
    StockListSpec,
)

#: 条件、排序要往前读的行情超过这么多条，提示上市不久的股票算不出来（默认剔除的次新股就是 60 个交易日）
LONG_LOOKBACK = 60

BASE_LABELS = {
    "all_a": "沪深A股（不含北交所）",
    "hs300": "沪深300成分股（按每天当时的成分）",
    "zz500": "中证500成分股（按每天当时的成分）",
}

BOARD_TYPE_LABELS = {"sw_industry": "申万一级行业", "concept": "通达信概念板块"}

BENCHMARK_LABELS = {
    "universe_equal_weight": "买入日全A等权平均（剔除 ST、停牌、次新股，持有期内退市的按最后价格算）",
    "index:000300.SH": "沪深300指数",
    "index:000905.SH": "中证500指数",
}

_NEW_LISTING = re.compile(r"new_listing_(\d+)d")


@dataclass(frozen=True)
class Mention:
    """用户原话里的一个说法，对应查询条件的哪个栏目（如「昨天」→ as_of、「放量」→ event）。"""

    phrase: str
    field: str


@dataclass(frozen=True)
class Facts:
    """生成说明要用、spec 自己算不出来的东西。"""

    #: 栏目 → 表达式的中文，如 "filter.expr" → "当日涨跌幅 > 9"
    expressions: Mapping[str, str] = dataclasses.field(default_factory=dict)
    uses_rank: bool = False
    #: 条件、排序里最长要往前读多少条行情
    lookback: int = 0
    stock_name: str | None = None
    #: 股票池限定的概念板块名和成分快照日
    board_name: str | None = None
    concept_snapshot: date | None = None
    #: 板块表：这种口径的数据区间
    board_range: tuple[date, date] | None = None
    #: 个股回看：回看区间裁到本地数据、扣掉预热期之后，实际从哪天算
    first_date: date | None = None
    mentions: tuple[Mention, ...] = ()


@dataclass(frozen=True)
class Assumption:
    field: str | None  # 对应的栏目；None 是整体说明
    text: str
    default: bool = False


def render_assumptions(
    spec: StockListSpec | BoardListSpec | StockHistorySpec, facts: Facts | None = None
) -> list[Assumption]:
    facts = facts or Facts()
    notes = _Notes(spec.defaults_used, facts.mentions)
    if isinstance(spec, StockHistorySpec):
        _history(spec, facts, notes)
    elif isinstance(spec, StockListSpec):
        _stock_list(spec, facts, notes)
    else:
        _board_list(spec, facts, notes)
    return notes.items


class _Notes:
    def __init__(self, defaults: tuple[str, ...], mentions: tuple[Mention, ...]):
        self.items: list[Assumption] = []
        self._defaults = defaults
        self._mentions = mentions

    def add(self, field: str, label: str, value: str, definition: str | None = None) -> None:
        """有原话说法时写成「「放量」理解为：定义」，definition 是去掉名称的定义，避免和原话重复。"""
        phrases = [m.phrase for m in self._mentions if _under(m.field, field)]
        if phrases:
            text = f"{'、'.join(f'「{p}」' for p in phrases)}理解为：{definition or value}"
        else:
            text = f"{label}：{value}"
        default = any(_under(path, field) for path in self._defaults)
        self.items.append(Assumption(field, text, default))

    def note(self, text: str) -> None:
        self.items.append(Assumption(None, text))


def _under(path: str, field: str) -> bool:
    return path == field or path.startswith(f"{field}.")


# ── 三种形状 ────────────────────────────────────────────────────


def _stock_list(spec: StockListSpec, facts: Facts, notes: _Notes) -> None:
    _day_and_conditions(spec, facts, notes)
    universe = spec.universe
    notes.add("universe.base", "股票池", BASE_LABELS[universe.base])
    if universe.industry:
        notes.add("universe.industry", "申万行业", f"{universe.industry}（按每个交易日当时的归属）")
    if universe.board:
        name = facts.board_name or universe.board.code
        notes.add("universe.board", "概念板块", name)
        notes.note(_board_members(name, facts.concept_snapshot, spec.as_of))
    excluded = "、".join(_exclude_label(value) for value in universe.exclude)
    notes.add("universe.exclude", "剔除", excluded or "不剔除")
    _expression_notes(facts, "当天的股票池（已按上面的范围和剔除项过滤）", notes)
    if facts.lookback > LONG_LOOKBACK:
        notes.note(
            f"条件要往前读 {facts.lookback} 个交易日的行情：上市不满这么久的股票算不出来，不会出现在结果里"
        )


def _board_list(spec: BoardListSpec, facts: Facts, notes: _Notes) -> None:
    scope = BOARD_TYPE_LABELS[spec.board_type]
    if facts.board_range:
        scope += f"，数据从 {facts.board_range[0]} 到 {facts.board_range[1]}"
    if spec.board_type == "concept":
        scope += "，只含现在还在的板块"
    notes.add("board_type", "板块口径", scope)
    _day_and_conditions(spec, facts, notes)
    _expression_notes(facts, "当天全部板块", notes)


def _history(spec: StockHistorySpec, facts: Facts, notes: _Notes) -> None:
    code = spec.target.code or ""
    notes.add("target", "股票", f"{facts.stock_name}（{code}）" if facts.stock_name else code)
    notes.add("event", "事件", spec.event.label or spec.event.expr)
    notes.note("事件只算由不满足变为满足的那一天，连续成立不重复计")
    window = f"{spec.time_range.start} ~ {spec.time_range.end}"
    if facts.first_date and facts.first_date > spec.time_range.start:
        window += f"，实际从 {facts.first_date} 算起（裁到本地数据、扣掉预热期之后）"
    notes.add("time_range", "回看区间", window)
    horizons = "、".join(str(h) for h in spec.horizons)
    notes.add("horizons", "持有天数", f"{horizons} 个交易日，从买入日起算")
    notes.note(
        "买入价是触发日下一个交易日的开盘价，开盘涨停或停牌就往后顺延；"
        "卖出价是持有期最后一天的收盘价，跌停或停牌就往后顺延"
    )
    notes.add("benchmark", "同期对照", BENCHMARK_LABELS[spec.benchmark])
    notes.add(
        "cost_bps",
        "交易成本",
        f"{spec.cost_bps / 100:.2f}%，买卖双边合计；平均涨跌不扣成本，另外单列扣掉成本后的数",
    )


# ── 零件 ────────────────────────────────────────────────────────


def _day_and_conditions(spec: StockListSpec | BoardListSpec, facts: Facts, notes: _Notes) -> None:
    notes.add("as_of", "日期", spec.as_of.isoformat())
    described = facts.expressions.get("filter.expr")
    notes.add(
        "filter",
        "筛选条件",
        _condition(spec.filter, described),
        _condition(spec.filter, described, labeled=False),
    )
    described = facts.expressions.get("sort.by")
    notes.add(
        "sort", "排序", _sort(spec.sort, described), _sort(spec.sort, described, labeled=False)
    )
    notes.add("limit", "取前", f"{spec.limit} 名")


def _condition(condition: Condition | None, described: str | None, labeled: bool = True) -> str:
    if condition is None:
        return "不筛选"
    text = described or condition.expr
    return f"{condition.label}（{text}）" if labeled and condition.label else text


def _sort(sort: Sort | None, described: str | None, labeled: bool = True) -> str:
    if sort is None:
        return "成交额从高到低（没有指定排序）"
    text = described or sort.by
    order = "从高到低" if sort.order == "desc" else "从低到高"
    return f"{sort.label}（{text}），{order}" if labeled and sort.label else f"{text}，{order}"


def _expression_notes(facts: Facts, rank_scope: str, notes: _Notes) -> None:
    if facts.uses_rank:
        notes.note(f"排名（Rank）是在{rank_scope}里排的分位，最高为 1")
    if facts.lookback:
        notes.note("「N 日」都按交易日数，停牌的日子不算")


def _board_members(name: str, snapshot: date | None, as_of: date) -> str:
    if snapshot is None:
        return f"「{name}」按当前成分筛选：当时在、现在不在的股票不会出现"
    if as_of < snapshot:
        return (
            f"「{name}」只有 {snapshot} 的成分：查 {as_of} 时，"
            "之后才调入的股票也算在内，当时在、后来调出的不会出现"
        )
    return f"「{name}」按 {snapshot} 的成分筛选"


def _exclude_label(value: str) -> str:
    if value == "ST":
        return "ST / *ST"
    if value == "suspended":
        return "停牌"
    days = _NEW_LISTING.fullmatch(value)
    return f"上市不满 {days.group(1)} 个交易日" if days else value
