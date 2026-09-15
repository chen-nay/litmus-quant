"""表达式 → 中文说明，给确认卡用（ARCHITECTURE §5.4）。纯代码翻译，不经过大模型：条件改了，说明跟着变。

- 字段用中文名：$amount → 成交额
- 全部算子都有对应说法；Mean(Ref(x, 1), n) 这种「前 n 日均值、不含当天」的常见写法单独翻得顺一点
- 运算先后和解析器一致（| < & < 比较 < 加减 < 乘除 < 取反），需要时加中文括号
- 一万以上的数按亿、万写：「总市值 < 30000000000」核对不了是 30 亿还是 300 亿
"""

from __future__ import annotations

from litmus.data import FIELDS, STOCK
from litmus.expr.parser import Binary, Call, Field, Node, Number, Unary, parse

_PRECEDENCE = {
    "|": 1,
    "&": 2,
    **dict.fromkeys((">", "<", ">=", "<=", "==", "!="), 3),
    "+": 4,
    "-": 4,
    "*": 5,
    "/": 5,
}
_UNARY, _ATOM = 6, 7

_SYMBOLS = {
    "|": "或",
    "&": "且",
    ">=": "≥",
    "<=": "≤",
    "==": "=",
    "!=": "≠",
    "*": "×",
    "/": "÷",
}

#: 带窗口、结果是一个统计量的算子
_WINDOW_WORDS = {"Mean": "均值", "Sum": "合计", "Max": "最高", "Min": "最低", "Std": "标准差"}


def describe(expr: str | Node, target: str = STOCK) -> str:
    """target 只影响 Rank 的说法：股票是在当天股票池里排，板块是在当天全部板块里排。"""
    node = parse(expr) if isinstance(expr, str) else expr
    return _Describer(target).text(node)


class _Describer:
    def __init__(self, target: str):
        self._target = target

    def text(self, node: Node) -> str:
        if isinstance(node, Number):
            return _number(node)
        if isinstance(node, Field):
            field = FIELDS.get(node.name)
            return field.label if field else f"${node.name}"
        if isinstance(node, Unary):
            if node.op == "~":
                return f"不满足{self._wrap(node.operand, _ATOM)}"
            return f"-{self._wrap(node.operand, _UNARY)}"
        if isinstance(node, Binary):
            level = _PRECEDENCE[node.op]
            left = self._wrap(node.left, level)
            # a - (b - c)、a / (b * c)：右边同级也要括号
            right = self._wrap(node.right, level + 1 if node.op in ("-", "/") else level)
            return f"{left} {_SYMBOLS.get(node.op, node.op)} {right}"
        return self._call(node)

    def _wrap(self, node: Node, minimum: int) -> str:
        text = self.text(node)
        return f"（{text}）" if _precedence(node) < minimum else text

    def _arg(self, node: Node) -> str:
        return self._wrap(node, _ATOM)

    def _value(self, node: Node) -> str:
        """紧跟在中文后面的数字前面空一格：「否则取 0」"""
        text = self._arg(node)
        return f" {text}" if isinstance(node, Number) else text

    def _call(self, node: Call) -> str:
        name, args = node.name, node.args
        if len(args) == 2 and name in _WINDOW_WORDS:
            subject, word, n = args[0], _WINDOW_WORDS[name], self.text(args[1])
            if (
                isinstance(subject, Call)
                and subject.name == "Ref"
                and len(subject.args) == 2
                and self.text(subject.args[1]) == "1"
            ):
                return f"前 {n} 日{self._arg(subject.args[0])}{word}（不含当天）"
            return f"近 {n} 日{self._arg(subject)}{word}"
        if len(args) == 2:
            x, y = self._arg(args[0]), self.text(args[1])
            if name == "Ref":
                return f"{'前一交易日' if y == '1' else f'{y} 个交易日前'}的{x}"
            if name == "EMA":
                return f"{x}的 {y} 日指数均线"
            if name == "Delta":
                return f"{x}较 {y} 个交易日前的变化"
            if name == "Pct":
                return f"{x}较 {y} 个交易日前的涨跌幅"
            if name == "TsRank":
                return f"{x}在近 {y} 日里的分位"
            if name == "Count":
                return f"近 {y} 日里{x}的天数"
            if name == "Cross":
                return f"{x}上穿{self._arg(args[1])}"
        if len(args) == 1:
            x = self._arg(args[0])
            if name == "Rank":
                scope = "当天股票池" if self._target == STOCK else "当天全部板块"
                return f"{x}在{scope}里的分位"
            if name == "Abs":
                return f"{x}的绝对值"
            if name == "Log":
                return f"{x}的自然对数"
            if name == "Sign":
                return f"{x}的正负（1、0、-1）"
        if name == "If" and len(args) == 3:
            condition, then, otherwise = (self._value(arg) for arg in args)
            return f"如果{condition}，取{then}，否则取{otherwise}"
        return f"{name}（{'，'.join(self.text(arg) for arg in args)}）"


def _number(node: Number) -> str:
    for unit, size in (("亿", 1e8), ("万", 1e4)):
        if abs(node.value) >= size:
            scaled = f"{node.value / size:.4f}".rstrip("0").rstrip(".")
            return f"{scaled} {unit}"
    return node.text


def _precedence(node: Node) -> int:
    if isinstance(node, Binary):
        return _PRECEDENCE[node.op]
    if isinstance(node, Unary):
        return _UNARY
    return _ATOM
