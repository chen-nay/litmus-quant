"""给大模型的字段清单与算子清单。

和校验器的白名单是同一份定义：字段来自 data.FIELDS，算子来自 operators.OPERATORS——
不会出现「清单里有、校验器却不认」或反过来的情况（ARCHITECTURE §3.3、§5）。
标的能不能用（概念板块要能力探测通过）由调用方先问 ds.available_targets()，不可用的标的不要给清单。
"""

from __future__ import annotations

from litmus.data import FIELDS
from litmus.expr.operators import (
    CROSS_SECTION,
    ELEMENTWISE,
    MAX_LOOKBACK,
    MAX_WINDOW,
    OPERATORS,
    TIMESERIES,
)
from litmus.expr.parser import Binary, Call, Field, Node, Number, parse

_KIND_LABELS = {TIMESERIES: "时序", CROSS_SECTION: "横截面", ELEMENTWISE: "逐行"}

#: 运算符与通用规则，和 parser / validator / logic 的行为一致
_RULES: tuple[tuple[str, str], ...] = (
    ("& | ~", "且、或、非，两边都要是条件"),
    (
        "> < >= <= == !=",
        "比较，比 & | 优先：$close > 10 & $pe_ttm < 20 就是两个条件同时成立；比较不能连写",
    ),
    ("+ - * /", "算术；除以 0 得到空值"),
    (
        "窗口天数 n",
        f"必须是正整数字面量，最大 {MAX_WINDOW}；数的是该标的自己有行情的交易日，停牌日不算",
    ),
    ("预热", f"整个表达式往前要读的行情不超过 {MAX_LOOKBACK} 条，EMA 按 8n 算"),
    ("空值", "亏损股的市盈率等为空值；空值参与的比较结果不确定，最后判为不满足"),
)


def field_catalog(target: str) -> list[dict[str, object]]:
    """某类标的能用的字段，按字段目录的顺序。"""
    return [
        {
            "name": f"${field.name}",
            "label": field.label,
            "unit": field.unit,
            "type": "条件" if field.dtype == "bool" else "数值",
            "note": field.note,
            "time_series_ok": field.time_series_ok,
        }
        for field in FIELDS.values()
        if field.available_for(target)
    ]


def operator_catalog() -> list[dict[str, str]]:
    """全部算子与运算规则。"""
    operators = [
        {"signature": op.signature, "label": op.label, "kind": _KIND_LABELS[op.kind]}
        for op in OPERATORS.values()
    ]
    rules = [{"signature": text, "label": label, "kind": "规则"} for text, label in _RULES]
    return operators + rules


#: 算出来是分位或涨跌幅的算子，结果是 0 附近的小数，页面按百分比显示
_RATIO_CALLS = frozenset({"Rank", "TsRank", "Pct", "PctSince"})

#: 分位、涨跌幅这类小数的单位标记，和 FIELDS 里的「%」区分开——那个已经是百分数了
RATIO = "小数百分比"


def result_unit(text: str | Node) -> str:
    """一个表达式算出来的数该怎么显示。

    - 就是一个字段：用那个字段的单位（`$market_cap` → 元，`$pct_chg` → %）
    - 顶上是 Rank / TsRank / Pct / PctSince：算出来是小数，按百分比显示
    - `x / y - 1`、`1 - x / y`：偏离、回撤这类比例，也按百分比显示（2026-09-18 实测「比最高点跌了多少」
      写成 `1 - $close / Max($close, 1000)`，卡上显示成 0.28）
    - 顶上是 Count：天数，按整数显示
    - 其余：没有单位，按普通数字显示
    """
    node = parse(text) if isinstance(text, str) else text
    if isinstance(node, Field):
        definition = FIELDS.get(node.name)
        return definition.unit if definition else ""
    if isinstance(node, Call) and node.name in _RATIO_CALLS:
        return RATIO
    if isinstance(node, Call) and node.name == "Count":
        return "个"
    if _relative(node):
        return RATIO
    return ""


def _relative(node: Node) -> bool:
    """`x / y - 1` 或 `1 - x / y`。"""
    if not (isinstance(node, Binary) and node.op == "-"):
        return False
    return (_divides(node.left) and _is_one(node.right)) or (
        _is_one(node.left) and _divides(node.right)
    )


def _divides(node: Node) -> bool:
    return isinstance(node, Binary) and node.op == "/"


def _is_one(node: Node) -> bool:
    return isinstance(node, Number) and node.value == 1
