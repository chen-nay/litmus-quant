"""从语法树推导需要哪些字段、往前要读多少条行情（预热）。

「LLM 不需要选 tool」就靠这两步：要什么数据、往前取多久，全部从表达式推出来（ARCHITECTURE §3.5）。

预热条数按精确条数算，这样「实际统计起点」和结果对得上：
- 并列取最大：`Mean($close, 250) > Ref($volume, 5)` → max(249, 5) = 249
- 嵌套要累加：`Mean(Ref($close, 5), 250)` → 5 + 249 = 254
- 含当天的 n 天窗口往前 n-1 条；`Ref / Delta / Pct` 往前 n 条；`Cross` 往前 1 条；EMA 按 8n
- 条数数的是每只标的自己的行情，不是交易日历上的天数

两个函数都假定表达式已经过校验。另外 compares_below 找「某字段小于某数」这一段，确认卡判断用没用默认门槛时用。
"""

from __future__ import annotations

from litmus.expr.operators import OPERATORS
from litmus.expr.parser import Binary, Call, Field, Node, Number, Unary


def collect_fields(node: Node) -> set[str]:
    """表达式用到的字段名（不带 $）。"""
    if isinstance(node, Field):
        return {node.name}
    if isinstance(node, Unary):
        return collect_fields(node.operand)
    if isinstance(node, Binary):
        return collect_fields(node.left) | collect_fields(node.right)
    if isinstance(node, Call):
        return set().union(*(collect_fields(arg) for arg in node.args))
    return set()


def compares_below(node: Node, field: str, value: float) -> bool:
    """表达式里有没有「$field 小于（等于）value」这一段，如 `$market_cap < 30亿`，也认反过来写的 `30亿 > $market_cap`。"""
    if isinstance(node, Binary):
        if node.op in ("<", "<=") and _bound(node.left, node.right, field, value):
            return True
        if node.op in (">", ">=") and _bound(node.right, node.left, field, value):
            return True
        return compares_below(node.left, field, value) or compares_below(node.right, field, value)
    if isinstance(node, Unary):
        return compares_below(node.operand, field, value)
    if isinstance(node, Call):
        return any(compares_below(arg, field, value) for arg in node.args)
    return False


def _bound(subject: Node, limit: Node, field: str, value: float) -> bool:
    return (
        isinstance(subject, Field)
        and subject.name == field
        and isinstance(limit, Number)
        and limit.value == value
    )


def collect_lookback(node: Node, *, ema_warmup: bool = True) -> int:
    """往前要读多少条行情。

    ema_warmup=False 用于上市以来的行情全在本地的标的：EMA 从上市第一天算起就是准的，不用额外预热。
    """
    if isinstance(node, Number | Field):
        return 0
    if isinstance(node, Unary):
        return collect_lookback(node.operand, ema_warmup=ema_warmup)
    if isinstance(node, Binary):
        return max(
            collect_lookback(node.left, ema_warmup=ema_warmup),
            collect_lookback(node.right, ema_warmup=ema_warmup),
        )
    op = OPERATORS[node.name]
    if op.name == "Cross":
        return 1 + max(collect_lookback(arg, ema_warmup=ema_warmup) for arg in node.args)
    if op.has_window and op.warmup is not None:
        subject, window = node.args
        assert isinstance(window, Number)
        own = 0 if (op.name == "EMA" and not ema_warmup) else op.warmup(int(window.value))
        return collect_lookback(subject, ema_warmup=ema_warmup) + own
    return max(collect_lookback(arg, ema_warmup=ema_warmup) for arg in node.args)
