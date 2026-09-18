"""确认卡的文案：QuerySpec → 一句话总结 + 按维度分组的说明（DESIGN.md §1）。

表和统计跑之前给用户核对，卡直接出结果、把这份说明放在卡底下的「怎么算的」里。
卡上只写用得着的：不排名就不写算的范围（池子影响不到结果），指标的中文和它的名字一样就不重复写。

文字全部从 spec 现生成，不存在「文字和参数对不上」。spec 自己算不出来的东西
（表达式的中文、池子多大、股票名、数据截至哪天）由上层查好放进 `Facts`，
所以这个模块不依赖 data、不依赖 expr。

用户原话里有说法时写成「「放量」理解为：定义」，没有就写「名称：值」。
用了默认值的那一行标出来，页面上跟在值后面显示。
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from litmus.spec.defaults import DEFAULTS
from litmus.spec.query import (
    CardOutput,
    EventStudyOutput,
    QuerySpec,
    Scope,
    TableOutput,
)

#: 分组，按这个顺序显示
WHO, WHEN, WHAT, HOW, EVENT, AFTER = "看谁", "看哪天", "看哪些数", "怎么出", "什么事件", "怎么算"

#: 剔除项的中文
_EXCLUDE = {"ST": "ST / *ST", "suspended": "停牌", "new_listing_60d": "上市不满 60 个交易日"}

#: 股票池底盘的中文
_BASE = {"all_a": "沪深A股（不含北交所）", "hs300": "沪深300 成分股", "zz500": "中证500 成分股"}

#: 标的类型的中文和量词
_TARGET = {
    "stock": ("股票", "只"),
    "sw_industry": ("申万一级行业", "个"),
    "sw_industry_l2": ("申万二级行业", "个"),
    "concept": ("通达信概念板块", "个"),
}

#: 指数对照的中文。等权平均按算的范围现写，见 _equal_weight
_BENCHMARK = {
    "index:000300.SH": "沪深300 指数",
    "index:000905.SH": "中证500 指数",
}


@dataclass(frozen=True)
class Mention:
    """用户原话里的说法挂在哪个栏目上。"""

    phrase: str
    field: str


@dataclass(frozen=True)
class Facts:
    """spec 自己算不出来的东西，由上层查好传进来。"""

    #: 每个 metric 的公式翻成中文：metric 名字 → 「收盘价较 2025-12-31 的涨跌幅」
    metric_texts: Mapping[str, str] = dataclasses.field(default_factory=dict)
    #: 筛选条件的中文
    filter_text: str = ""
    #: scope 圈出来多少个标的
    pool_size: int | None = None
    #: scope 限定的申万行业是哪一级：「申万一级行业」「申万二级行业，属于电子」
    industry_scope: str = ""
    #: subject=codes 时每个代码对应的名字
    names: Mapping[str, str] = dataclasses.field(default_factory=dict)
    #: scope 限定的概念板块名和成分快照日
    board_name: str | None = None
    concept_snapshot: date | None = None
    #: 板块数据的可用区间
    board_range: tuple[date, date] | None = None
    #: 事件统计裁到本地数据、扣掉预热期之后，实际从哪天算
    first_date: date | None = None
    #: 条件、指标里最长要往前读多少条行情
    lookback: int = 0
    uses_rank: bool = False
    mentions: tuple[Mention, ...] = ()
    #: 用了默认门槛的栏目（「小市值」→ 总市值低于 30 亿）
    defaulted: frozenset[str] = frozenset()
    #: 这个问题用到的几类数据各自截至哪天：(叫法, 日期)
    data_dates: tuple[tuple[str, date], ...] = ()
    #: 条件、指标里用到了上市时间（$list_days、$is_new）
    uses_listing: bool = False


@dataclass(frozen=True)
class Assumption:
    group: str  # WHO / WHEN / …；空串是不分组的整体说明
    field: str | None  # 对应的栏目，用来标默认值和挂原话
    text: str
    default: bool = False


@dataclass(frozen=True)
class Confirm:
    summary: str
    items: tuple[Assumption, ...]


def render_confirm(spec: QuerySpec, facts: Facts | None = None) -> Confirm:
    facts = facts or Facts()
    notes = _Notes((*spec.defaults_used, *facts.defaulted), facts.mentions)
    card = isinstance(spec.output, CardOutput)
    _who(spec, facts, notes, card)
    _when(spec, facts, notes)
    if spec.metrics:
        _what(spec, facts, notes, card)
    if isinstance(spec.output, EventStudyOutput):
        _event(spec.output, facts, notes)
        _after(spec, facts, notes)
    elif isinstance(spec.output, TableOutput):
        _how(spec.output, facts, notes)
    if facts.data_dates:
        dates = "，".join(f"{label} {day}" for label, day in facts.data_dates)
        notes.note(f"本地数据截至：{dates}", group="-")
    return Confirm(summary=summarize(spec, facts), items=tuple(notes.items))


# ── 一句话总结 ──────────────────────────────────────────────────


def summarize(spec: QuerySpec, facts: Facts | None = None) -> str:
    facts = facts or Facts()
    where = _scope_phrase(spec.scope, facts)
    if isinstance(spec.output, EventStudyOutput):
        who = _subject_phrase(spec, facts) or where
        horizons = "、".join(str(n) for n in spec.output.horizons)
        label = spec.output.event.label or "这个事件"
        return f"{who}历史上每次{label}之后，接下来 {horizons} 个交易日涨跌多少"
    if isinstance(spec.output, CardOutput):
        return f"{_subject_phrase(spec, facts) or where}的{'、'.join(m.name for m in spec.metrics)}"
    kind, unit = _TARGET[spec.scope.target]
    sort = spec.output.sort
    by = f"「{sort.by}」" if sort else "成交额"
    order = "从高到低" if sort is None or sort.order == "desc" else "从低到高"
    narrowed = _filter_phrase(spec.output, facts)
    what = (
        f"{spec.output.limit} {unit}股票"
        if spec.scope.target == "stock"
        else f"{spec.output.limit} {unit}"
    )
    return f"{where}里{f'{narrowed}的' if narrowed else ''}，按 {by} {order}取前 {what}"


def _filter_phrase(output: TableOutput, facts: Facts) -> str:
    """筛选条件在总结里用中文说法，没有说法就不提。"""
    if output.filter is None:
        return ""
    return output.filter.label or facts.filter_text or ""


def _scope_phrase(scope: Scope, facts: Facts) -> str:
    if scope.industry:
        return scope.industry
    if scope.board is not None:
        return facts.board_name or scope.board.code
    if scope.target != "stock":
        return _TARGET[scope.target][0]
    return _BASE[scope.base]


def _subject_phrase(spec: QuerySpec, facts: Facts) -> str:
    if spec.subject.kind != "codes":
        return ""
    named = [facts.names.get(code, code) for code in spec.subject.codes]
    return "、".join(named) if named else "、".join(m.mention for m in spec.subject.mentions)


# ── 看谁 ────────────────────────────────────────────────────────


def _who(spec: QuerySpec, facts: Facts, notes: _Notes, card: bool = False) -> None:
    scope, subject = spec.scope, spec.subject
    kind, unit = _TARGET[scope.target]

    if card:
        # 卡的标题上就是看谁；不排名时算的范围影响不到卡上任何一个数，不写
        if facts.uses_rank:
            _scope(spec, facts, notes)
        return
    if subject.kind == "codes":
        label = "股票" if scope.target == "stock" else "板块"
        for code in subject.codes:
            name = facts.names.get(code)
            notes.add(WHO, "subject", label, f"{name}（{code}）" if name else code)
        if not subject.codes:
            for mention in subject.mentions:
                notes.add(WHO, "subject", label, mention.mention)
    elif subject.kind == "aggregate":
        notes.note(f"{_scope_phrase(scope, facts)}整体，算成一个数", group=WHO)
    _scope(spec, facts, notes)


def _scope(spec: QuerySpec, facts: Facts, notes: _Notes) -> None:
    """算的范围：点名看某几个时也要写，Rank、全A等权都在这个范围里算。"""
    scope = spec.scope
    kind, unit = _TARGET[scope.target]
    size = f"，{facts.pool_size} {unit}" if facts.pool_size is not None else ""
    detail = "，".join(x for x in (facts.industry_scope, size.lstrip("，")) if x)
    if scope.industry:
        notes.add(
            WHO,
            "scope.industry",
            "算的范围",
            f"{scope.industry}{f'（{detail}）' if detail else ''}",
        )
        notes.note("按每个交易日当时的归属取成分", group=WHO)
    elif scope.board is not None:
        name = facts.board_name or scope.board.code
        notes.add(WHO, "scope.board", "算的范围", f"{name}{size}")
        if facts.concept_snapshot:
            notes.note(f"成分按 {facts.concept_snapshot} 的快照，已经撤销的板块不在里面", group=WHO)
    elif scope.target != "stock":
        notes.add(WHO, "scope.target", "算的范围", f"{kind}{size}")
        notes.note("只含现存的板块，已经撤销的不在里面", group=WHO)

    if scope.target == "stock":
        base = f"{_BASE[scope.base]}{size if not (scope.industry or scope.board) else ''}"
        notes.add(WHO, "scope.base", "股票池", base)
        dropped = "、".join(_EXCLUDE.get(name, name) for name in scope.exclude)
        notes.add(WHO, "scope.exclude", "剔除", dropped or "不剔除")
        new_listing = [name for name in scope.exclude if name.startswith("new_listing_")]
        if facts.uses_listing and new_listing:
            notes.note(
                f"条件里用到了上市时间，但{_EXCLUDE.get(new_listing[0], new_listing[0])}的股票已经剔除了："
                "要看次新股，把这一项去掉",
                group=WHO,
            )


# ── 看哪天 ──────────────────────────────────────────────────────


def _when(spec: QuerySpec, facts: Facts, notes: _Notes) -> None:
    when = spec.when
    if when.range is not None:
        text = f"{when.range.start} ~ {when.range.end}"
        if facts.first_date and facts.first_date > when.range.start:
            text += f"，实际从 {facts.first_date} 算起（裁到本地数据、扣掉预热期之后）"
        notes.add(WHEN, "when.range", "回看区间", text)
    elif when.as_of is not None:
        notes.add(WHEN, "when.as_of", "日期", str(when.as_of))
    if facts.board_range:
        first, last = facts.board_range
        notes.note(f"这类数据从 {first} 起，问更早的答不了", group=WHEN)


# ── 看哪些数 ────────────────────────────────────────────────────


def _what(spec: QuerySpec, facts: Facts, notes: _Notes, card: bool = False) -> None:
    for metric in spec.metrics:
        text = facts.metric_texts.get(metric.name, metric.expr)
        if card and text == metric.name:
            continue  # 「市盈率TTM：市盈率TTM」，写了等于没写
        notes.add(WHAT, f"metrics.{metric.name}", metric.name, text)
    if card:
        # 排名怎么排写在指标那一行；算不出来的卡上逐条说了原因，不用再提预热
        benchmark = getattr(spec.output, "benchmark", "industry")
        if benchmark in _BENCHMARK:
            notes.add(WHAT, "output.benchmark", "涨跌的同期对照", _BENCHMARK[benchmark])
        return
    if facts.uses_rank:
        notes.note("排名在上面「算的范围」里排，不是全市场", group=WHAT)
    if facts.lookback:
        notes.note(
            f"要往前读 {facts.lookback} 个交易日的行情：上市不满这么久的算不出来，不会出现在结果里",
            group=WHAT,
        )


# ── 怎么出 ──────────────────────────────────────────────────────


def _how(output: TableOutput, facts: Facts, notes: _Notes) -> None:
    if output.filter is not None:
        notes.add(HOW, "output.filter", "先筛", facts.filter_text or output.filter.expr)
    else:
        notes.note("不筛选", group=HOW)
    if output.sort is not None:
        order = "从高到低" if output.sort.order == "desc" else "从低到高"
        notes.add(HOW, "output.sort", "排序", f"按「{output.sort.by}」{order}")
    else:
        notes.note("没有指定排序，按成交额从高到低排", group=HOW)
    notes.add(HOW, "output.limit", "取前", f"{output.limit} 名")


# ── 什么事件 / 之后怎么算 ───────────────────────────────────────


def _event(output: EventStudyOutput, facts: Facts, notes: _Notes) -> None:
    notes.add(EVENT, "output.event", "事件", output.event.label or output.event.expr)
    notes.note("只算由不满足变为满足的那一天，连续成立不重复计", group=EVENT)


def _after(spec: QuerySpec, facts: Facts, notes: _Notes) -> None:
    output = spec.output
    assert isinstance(output, EventStudyOutput)
    horizons = "、".join(str(n) for n in output.horizons)
    notes.add(AFTER, "output.horizons", "持有天数", f"{horizons} 个交易日，从买入日起算")
    notes.note(
        "买入价是触发日下一个交易日的开盘价，开盘涨停或停牌就往后顺延；"
        "卖出价是持有期最后一天的收盘价，跌停或停牌就往后顺延",
        group=AFTER,
    )
    benchmark = _BENCHMARK.get(output.benchmark) or _equal_weight(spec.scope, facts)
    notes.add(AFTER, "output.benchmark", "同期对照", benchmark)
    percent = f"{output.cost_bps / 100:.2f}%"
    notes.add(
        AFTER,
        "output.cost_bps",
        "交易成本",
        f"{percent}，买卖双边合计；平均涨跌不扣成本，另外单列扣掉成本后的数",
    )


def _equal_weight(scope: Scope, facts: Facts) -> str:
    """等权平均的口径就是算的范围：「买入日全A等权平均（剔除 ……）」「买入日农林牧渔等权平均（……）」。"""
    whole = scope.target == "stock" and scope.base == "all_a" and not scope.industry
    where = "全A" if whole and scope.board is None else _scope_phrase(scope, facts)
    dropped = "、".join(_EXCLUDE.get(name, name) for name in scope.exclude)
    return f"买入日{where}等权平均（{f'剔除 {dropped}，' if dropped else ''}持有期内退市的按最后价格算）"


# ── 拼文字 ──────────────────────────────────────────────────────


class _Notes:
    def __init__(self, defaults: tuple[str, ...], mentions: tuple[Mention, ...]):
        self.items: list[Assumption] = []
        self._defaults = defaults
        self._mentions = mentions
        self._group = ""

    def add(self, group: str, field: str, label: str, value: str) -> None:
        """有原话说法时写成「「放量」理解为：定义」，否则「名称：值」。"""
        phrases = [m.phrase for m in self._mentions if _under(m.field, field)]
        if phrases and not _repeats(phrases, value):
            text = f"{'、'.join(f'「{p}」' for p in phrases)}理解为：{value}"
        else:
            text = f"{label}：{value}"
        self.items.append(Assumption(group, field, text, _is_default(self._defaults, field)))
        self._group = group

    def note(self, text: str, group: str = "") -> None:
        """group 留空跟着上一条；传 "-" 表示不属于任何一组。"""
        self.items.append(Assumption("" if group == "-" else (group or self._group), None, text))


def _is_default(defaults: tuple[str, ...], field: str) -> bool:
    """两个方向都算：默认值记的是「when」，说明挂在「when.as_of」上，反过来也一样。"""
    return any(_under(path, field) or _under(field, path) for path in defaults)


def _under(path: str, field: str) -> bool:
    return path == field or path.startswith(f"{field}.")


def _repeats(phrases: list[str], value: str) -> bool:
    """原话就是说明开头的名称时，再写「理解为」就重复了。"""
    return (
        len(phrases) == 1 and re.match(rf"{re.escape(phrases[0])}(?:$|[，（的])", value) is not None
    )


def small_cap_note() -> str:
    """「小市值」用了默认门槛时的说法，页面和提示词共用一份。"""
    return f"总市值低于 {int(DEFAULTS['small_cap']) // 100_000_000} 亿"  # type: ignore[call-overload]


def group_order() -> Sequence[str]:
    return (WHO, WHEN, WHAT, HOW, EVENT, AFTER, "")
