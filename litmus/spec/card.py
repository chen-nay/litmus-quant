"""卡上的文字：每个指标的值怎么写、下面那行解释怎么写（DESIGN.md §1.5）。

和确认卡（confirm.py）同一套做法：数由上层算好、空值的原因由上层查好，放进 `MetricFacts`；
这里只管拼成话，不依赖 data、不依赖 expr。

解释行按指标的类型写：

    分位   说高低和这段时间的区间；分位的那个数也在卡上时，分位写进那个数的解释行，不单独占一行
    涨跌   给同期行业指数做对照
    排名   写成「第几名（共几只）」，解释行写被排的那个数
    条件   写比较的两边
    空值   说清为什么空，不能只显示「无」

数字的写法和结果表一致：金额按亿、万，涨跌和同比带正负号，保留两位小数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from litmus.spec.defaults import DEFAULTS

#: 指标类型
VALUE, CHANGE, PERCENTILE, RANK, CONDITION = "value", "change", "percentile", "rank", "condition"

#: 空值的原因
NOT_LISTED = "not_listed"  # 当天还没上市
DELISTED = "delisted"  # 已经退市
SUSPENDED = "suspended"  # 当天停牌
NO_QUOTE = "no_quote"  # 板块当天没有行情
EXCLUDED = "excluded"  # 被算的范围剔除，不参与排名
LOSS = "loss"  # 亏损，没有市盈率
NO_REPORT = "no_report"  # 还没公告过财报
NO_VALUE = "no_value"  # 当天没有某个字段
NO_START = "no_start"  # 涨跌的起点那天还没上市
SHORT = "short"  # 行情不够长
GAPS = "gaps"  # 窗口里有空值
NOT_FINITE = "not_finite"  # 除以 0 之类

#: 小数，按百分比显示（和 expr.RATIO 同一个标记）
RATIO = "小数百分比"
EMPTY = "无"

_MONEY_FIELDS = frozenset({"amount", "market_cap", "circ_mv"})
_SIGNED_FIELDS = frozenset({"pct_chg", "revenue_yoy", "profit_yoy"})
_CHINESE = {1: "一", 2: "两", 3: "三", 4: "四"}


@dataclass(frozen=True)
class Report:
    """截至当天最新一期财报。"""

    period: date  # 报告期
    roe: float | None
    profit_yoy: float | None


@dataclass(frozen=True)
class Missing:
    """值为空的原因。"""

    reason: str
    #: 缺的是哪个数：「市盈率TTM」
    label: str = ""
    #: 上市日、退市日、停牌前最后一个交易日、涨跌的起点
    day: date | None = None
    #: SHORT / GAPS：要最近多少个交易日；实际有几个 / 其中几天为空
    need: int = 0
    have: int = 0
    report: Report | None = None
    #: EXCLUDED：被剔除的原因，接在「当天」后面：「是 ST 股」「上市不满 60 个交易日」
    excluded: str = ""


@dataclass(frozen=True)
class MetricFacts:
    """卡上一个数要显示的全部东西，由上层查好。"""

    kind: str
    value: float | bool | None
    unit: str = ""
    #: 公式就是一个字段时的字段名，决定金额、涨跌的写法
    field: str | None = None
    #: 这个数叫什么（公式的中文），分位、排名、条件的两边用得上
    label: str = ""
    missing: Missing | None = None
    #: 后复权价：当天的实际收盘价，不是收盘价就是 None
    adjusted: bool = False
    raw_close: float | None = None
    #: 涨跌：算的是哪一段（「今年以来」「近 20 个交易日」），同期对照（名字, 涨跌）
    period: str = ""
    benchmark: tuple[str, float | None] | None = None
    #: 分位：窗口多少个交易日，窗口里最低、最高，其中有值的天数（没有值的那些天不算）
    window: int = 0
    low: float | None = None
    high: float | None = None
    counted: int = 0
    #: 排名：从高到低第几、一共几个、有没有并列、量词
    position: int | None = None
    total: int = 0
    tied: bool = False
    measure: str = "只"
    #: 分位、排名里面被算的那个数：TsRank($pb, 500) 里的市净率
    inner: MetricFacts | None = None
    #: 条件的两边（数字常量那边不列）
    sides: tuple[MetricFacts, ...] = ()
    #: 两边都是后复权价：写高低百分之几，不写价格
    relative: bool = False
    #: 这个数自己的分位也在卡上时，并进来写在解释行里
    percentile: MetricFacts | None = None


def render_row(facts: MetricFacts) -> tuple[str, str]:
    """(值, 解释行)。没什么可解释的解释行为空串。"""
    return display(facts), _note(facts)


# ── 值 ──────────────────────────────────────────────────────────


def display(facts: MetricFacts) -> str:
    value = facts.value
    if facts.kind == RANK:
        if facts.position is None:
            return EMPTY
        tied = "并列" if facts.tied else ""
        return f"{tied}第 {facts.position} 名（共 {facts.total} {facts.measure}）"
    if facts.kind == PERCENTILE:
        return EMPTY if value is None else _percentile(value)
    return format_value(value, facts.unit, facts.field)


def format_value(value: float | bool | None, unit: str = "", field: str | None = None) -> str:
    """和结果表的格子同一套写法（web/src/format.ts 的 formatCell）。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return EMPTY
    if field in _MONEY_FIELDS:
        return _yuan(value)
    if unit == RATIO:
        return _percent(value * 100, sign=True)
    if unit == "%":
        return _percent(value, sign=field in _SIGNED_FIELDS)
    if unit in ("个", "股"):
        return _fixed(value, 0)
    return _fixed(value, 2)


def _fixed(value: float, digits: int) -> str:
    text = f"{value:.{digits}f}"
    return f"{0:.{digits}f}" if float(text) == 0 else text


def _percent(value: float, sign: bool = False) -> str:
    text = _fixed(value, 2)
    return f"+{text}%" if sign and float(text) > 0 else f"{text}%"


def _percentile(value: float) -> str:
    return f"{round(value * 100)}%"


def _yuan(value: float) -> str:
    size = abs(value)
    if size >= 1e8:
        return f"{_fixed(value / 1e8, 2)}亿"
    if size >= 1e4:
        return f"{_fixed(value / 1e4, 2)}万"
    return _fixed(value, 2)


# ── 解释行 ──────────────────────────────────────────────────────


def _note(facts: MetricFacts) -> str:
    if facts.kind == PERCENTILE:
        return _percentile_note(facts)
    if facts.kind == RANK:
        return _rank_note(facts)
    if facts.missing is not None:
        text = missing_text(facts.missing)
        if facts.percentile is not None:
            text += f"，{window_word(facts.percentile.window)}分位也算不出"
        return text
    parts = []
    if facts.kind == VALUE and facts.adjusted:
        parts.append(
            "后复权价"
            if facts.raw_close is None
            else f"后复权价，当天实际收盘价 {_fixed(facts.raw_close, 2)} 元"
        )
    if facts.kind == CHANGE and facts.benchmark is not None:
        name, change = facts.benchmark
        if change is not None:
            parts.append(f"同期{name} {format_value(change, RATIO)}")
    if facts.kind == CONDITION:
        parts.append(_sides(facts))
    if facts.percentile is not None:
        parts.append(_folded_percentile(facts.percentile))
    return "；".join(part for part in parts if part)


def _percentile_note(facts: MetricFacts) -> str:
    """分位自己占一行：说高低，再给被算的那个数这段时间的区间和现在的值。"""
    if facts.missing is not None:
        return f"{missing_text(facts.missing)}，算不出分位"
    assert facts.value is not None
    text = level_word(float(facts.value)) + _skipped(facts)
    inner = facts.inner
    if inner is None or facts.low is None or facts.high is None:
        return text
    span = _span(inner, facts.low, facts.high)
    current = format_value(inner.value, inner.unit, inner.field)
    return f"{text}；{inner.label}{window_word(facts.window)}区间 {span}，现在 {current}"


def _folded_percentile(facts: MetricFacts) -> str:
    """并进那个数的解释行：「两年分位 28%，偏低；两年区间 2.17 ~ 4.15」。"""
    window = window_word(facts.window)
    if facts.missing is not None:
        return f"{window}分位算不出：{missing_text(facts.missing)}"
    assert facts.value is not None
    value = float(facts.value)
    text = f"{window}分位 {_percentile(value)}，{level_word(value)}{_skipped(facts)}"
    if facts.inner is not None and facts.low is not None and facts.high is not None:
        text += f"；{window}区间 {_span(facts.inner, facts.low, facts.high)}"
    return text


def _skipped(facts: MetricFacts) -> str:
    """窗口里有几天没有值、没算进分位：「（两年里 379 天有市盈率TTM，亏损的 121 天不算）」。"""
    inner = facts.inner
    skipped = facts.window - facts.counted
    if inner is None or not facts.counted or skipped <= 0:
        return ""
    without = "亏损的" if inner.field == "pe_ttm" else f"没有{inner.label}的"
    window = window_word(facts.window)
    return f"（{window}里 {facts.counted} 天有{inner.label}，{without} {skipped} 天不算）"


def _span(inner: MetricFacts, low: float, high: float) -> str:
    return f"{format_value(low, inner.unit, inner.field)} ~ {format_value(high, inner.unit, inner.field)}"


def _rank_note(facts: MetricFacts) -> str:
    """排名的解释行写被排的那个数：「今年以来 -17.89%，同期农林牧渔行业指数 -14.52%」。"""
    if facts.missing is not None:
        return f"{missing_text(facts.missing)}，不参与排名"
    inner = facts.inner
    if inner is None:
        return ""
    name = inner.period if inner.kind == CHANGE and inner.period else inner.label
    text = f"{name} {display(inner)}"
    detail = _note(inner) if inner.kind == CHANGE else ""
    return f"{text}，{detail}" if detail else text


def _sides(facts: MetricFacts) -> str:
    """条件的两边：两边都是后复权价时说高低百分之几，否则各写各的值。"""
    if facts.relative and len(facts.sides) == 2:
        left, right = facts.sides
        if isinstance(left.value, int | float) and isinstance(right.value, int | float):
            if right.value:
                gap = float(left.value) / float(right.value) - 1
                word = "高" if gap >= 0 else "低"
                return f"{left.label}比{right.label}{word} {_percent(abs(gap) * 100)}"
    return "，".join(
        f"{side.label} {format_value(side.value, side.unit, side.field)}" for side in facts.sides
    )


def missing_text(missing: Missing) -> str:
    reason = missing.reason
    if reason == NOT_LISTED:
        return f"当天还没上市，{missing.day} 才上市"
    if reason == DELISTED:
        return f"{missing.day} 已经退市"
    if reason == SUSPENDED:
        last = f"，停牌前最后一个交易日是 {missing.day}" if missing.day else ""
        return f"当天停牌，没有行情{last}"
    if reason == NO_QUOTE:
        return "当天没有行情"
    if reason == EXCLUDED:
        return f"当天{missing.excluded}，算的范围把它剔除了"
    if reason == LOSS:
        return _loss(missing.report)
    if reason == NO_REPORT:
        return "还没有公告过财报"
    if reason == NO_VALUE:
        return f"当天没有{missing.label}"
    if reason == NO_START:
        return f"{missing.day} 时还没上市，没有起点"
    if reason == SHORT:
        return f"要最近 {missing.need} 个交易日的行情，只有 {missing.have} 个"
    if reason == GAPS:
        return f"最近 {missing.need} 个交易日里有 {missing.have} 天没有{missing.label}"
    return "算出来不是有限的数（比如除以 0）"


def _loss(report: Report | None) -> str:
    """市盈率为空就是过去四个季度合计亏损。最新一期本身也亏损时，把那一期写出来。"""
    if report is None or report.roe is None or report.roe >= 0:
        return "过去四个季度合计亏损，亏损股没有市盈率"
    growth = (
        f"（净利同比 {format_value(report.profit_yoy, '%', 'profit_yoy')}）"
        if report.profit_yoy is not None
        else ""
    )
    return f"{period_word(report.period)}亏损{growth}，亏损股没有市盈率"


# ── 词 ──────────────────────────────────────────────────────────


def period_word(period: date) -> str:
    """报告期 → 「2026 上半年」。"""
    words = {3: "一季度", 6: "上半年", 9: "前三季度", 12: "全年"}
    return f"{period.year} {words.get(period.month, f'截至 {period}')}"


def window_word(n: int) -> str:
    """窗口天数 → 「两年」「三个月」，对不上整数的写「近 130 个交易日」。"""
    year = int(DEFAULTS["year_days"])  # type: ignore[call-overload]
    named = {
        int(DEFAULTS["week_days"]): "一周",  # type: ignore[call-overload]
        int(DEFAULTS["month_days"]): "一个月",  # type: ignore[call-overload]
        int(DEFAULTS["quarter_days"]): "三个月",  # type: ignore[call-overload]
        year // 2: "半年",
        **{year * k: f"{word}年" for k, word in _CHINESE.items()},
    }
    return named.get(n, f"近 {n} 个交易日")


def level_word(value: float) -> str:
    """分位 → 高低。"""
    if value < 0.2:
        return "处在低位"
    if value < 0.4:
        return "偏低"
    if value <= 0.6:
        return "处在中间"
    if value <= 0.8:
        return "偏高"
    return "处在高位"
