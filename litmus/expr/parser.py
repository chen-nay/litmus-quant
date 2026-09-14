"""表达式解析：字符串 → 语法树。

自己写的递归下降解析器，不引入依赖，也不执行任何代码。优先级从低到高（ARCHITECTURE §3.3）：

    |  →  &  →  比较（< <= > >= == !=，不能连写）  →  + -  →  * /  →  一元 ~ -  →  数字、$字段、算子调用、括号

**比较比 & | 优先，和 SQL 一样**。大模型常写 `$close > 10 & $pe_ttm < 20`，按 Python 的规则 `&` 更优先，
会被理解成 `$close > (10 & $pe_ttm) < 20`，悄悄算错；加了括号的写法在这套规则下含义不变。

报错带上字符位置和中文原因，大模型按报错修改表达式时用得上。字段、算子是否存在由校验器管，这里只管语法。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 表达式最长多少个字符。正常条件几十个字符，太长多半是大模型写跑了
MAX_LENGTH = 500

COMPARISONS = frozenset({"<", "<=", ">", ">=", "==", "!="})


class ExprSyntaxError(ValueError):
    """表达式写法不对。position 从 0 数起，message 是不带位置的中文原因。"""

    def __init__(self, message: str, position: int):
        super().__init__(f"第 {position + 1} 个字符：{message}")
        self.message = message
        self.position = position


# ── 语法树 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Number:
    value: float
    text: str  # 原文，用来判断是不是整数字面量（窗口参数只收整数）
    position: int

    @property
    def is_int(self) -> bool:
        return self.text.isdigit()


@dataclass(frozen=True)
class Field:
    name: str  # 不带 $
    position: int


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Node, ...]
    position: int


@dataclass(frozen=True)
class Unary:
    op: str  # ~ 或 -
    operand: Node
    position: int


@dataclass(frozen=True)
class Binary:
    op: str
    left: Node
    right: Node
    position: int


Node = Number | Field | Call | Unary | Binary


def onset(node: Node) -> Node:
    """事件只取「由不满足变为满足」的那一天：cond & ~Ref(cond, 1)（ARCHITECTURE §4.3）。

    连续 5 天创新高只算 1 次，三连板只在第 2 天触发一次。在语法树上包、不拼字符串：
    拼字符串会让表达式长度翻倍，长一点的事件就超过 MAX_LENGTH。预热条数随之多 1。
    """
    at = node.position
    previous = Call("Ref", (node, Number(1.0, "1", at)), at)
    return Binary("&", node, Unary("~", previous, at), at)


# ── 词法 ────────────────────────────────────────────────────────


_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<number>(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<field>\$[A-Za-z_][A-Za-z0-9_]*)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|==|!=|[<>&|~+\-*/(),])
    """,
    re.VERBOSE,
)

#: 常见的误写 → 提示
_CHAR_HINTS = {
    "=": "判断相等要写 ==",
    "!": "取反要写 ~，不等于要写 !=",
    "$": "字段名要紧跟在 $ 后面，如 $close",
    "[": "取前几天的值用 Ref(x, n)",
    "]": "取前几天的值用 Ref(x, n)",
    "（": "括号要用英文半角 ( )",
    "）": "括号要用英文半角 ( )",
    "，": "逗号要用英文半角 ,",
}


@dataclass(frozen=True)
class _Token:
    kind: str  # number / field / name / op
    text: str
    position: int


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            char = text[position]
            hint = _CHAR_HINTS.get(char)
            raise ExprSyntaxError(
                f"不认识的字符「{char}」" + (f"：{hint}" if hint else ""), position
            )
        if match.lastgroup != "space":
            tokens.append(_Token(match.lastgroup or "", match.group(), position))
        position = match.end()
    return tokens


# ── 语法 ────────────────────────────────────────────────────────


def parse(text: str) -> Node:
    """解析表达式。写法不对抛 ExprSyntaxError。"""
    if len(text) > MAX_LENGTH:
        raise ExprSyntaxError(f"表达式有 {len(text)} 个字符，超过上限 {MAX_LENGTH}", MAX_LENGTH)
    if not text.strip():
        raise ExprSyntaxError("表达式是空的", 0)
    parser = _Parser(_tokenize(text), len(text))
    node = parser.expression()
    parser.expect_end()
    return node


class _Parser:
    def __init__(self, tokens: list[_Token], length: int):
        self._tokens = tokens
        self._index = 0
        self._length = length

    # 每一级只认自己的运算符，操作数交给更高一级

    def expression(self) -> Node:
        return self._binary_level(("|",), self._and)

    def _and(self) -> Node:
        return self._binary_level(("&",), self._comparison)

    def _comparison(self) -> Node:
        node = self._additive()
        token = self._peek()
        if token is not None and token.text in COMPARISONS:
            self._index += 1
            node = Binary(token.text, node, self._additive(), token.position)
            following = self._peek()
            if following is not None and following.text in COMPARISONS:
                raise ExprSyntaxError(
                    "比较不能连写：1 < x < 2 要写成 (x > 1) & (x < 2)", following.position
                )
        return node

    def _additive(self) -> Node:
        return self._binary_level(("+", "-"), self._multiplicative)

    def _multiplicative(self) -> Node:
        return self._binary_level(("*", "/"), self._unary)

    def _binary_level(self, ops: tuple[str, ...], operand) -> Node:
        node = operand()
        while (token := self._peek()) is not None and token.text in ops:
            self._index += 1
            node = Binary(token.text, node, operand(), token.position)
        return node

    def _unary(self) -> Node:
        token = self._peek()
        if token is not None and token.kind == "op" and token.text in ("~", "-"):
            self._index += 1
            return Unary(token.text, self._unary(), token.position)
        return self._primary()

    def _primary(self) -> Node:
        token = self._peek()
        if token is None:
            raise ExprSyntaxError("表达式没写完，结尾还缺东西", self._length)
        self._index += 1
        if token.kind == "number":
            return Number(float(token.text), token.text, token.position)
        if token.kind == "field":
            return Field(token.text[1:], token.position)
        if token.kind == "name":
            return self._call(token)
        if token.text == "(":
            node = self.expression()
            self._expect(")", "括号没有配对，缺右括号")
            return node
        raise ExprSyntaxError(f"这里不该出现「{token.text}」", token.position)

    def _call(self, name: _Token) -> Call:
        if not self._accept("("):
            if name.text.lower() in ("and", "or", "not"):
                raise ExprSyntaxError(f"「{name.text}」要写成 & | ~", name.position)
            raise ExprSyntaxError(
                f"「{name.text}」后面缺括号：字段要写成 ${name.text}，算子要写成 {name.text}(...)",
                name.position,
            )
        args: list[Node] = []
        if not self._accept(")"):
            args.append(self.expression())
            while self._accept(","):
                args.append(self.expression())
            self._expect(")", f"{name.text}(...) 缺右括号")
        return Call(name.text, tuple(args), name.position)

    # 小工具

    def _peek(self) -> _Token | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _accept(self, text: str) -> bool:
        token = self._peek()
        if token is not None and token.kind == "op" and token.text == text:
            self._index += 1
            return True
        return False

    def _expect(self, text: str, message: str) -> None:
        if not self._accept(text):
            token = self._peek()
            raise ExprSyntaxError(message, token.position if token else self._length)

    def expect_end(self) -> None:
        token = self._peek()
        if token is None:
            return
        if token.kind == "name" and token.text.lower() in ("and", "or", "not"):
            raise ExprSyntaxError(f"「{token.text}」要写成 & | ~", token.position)
        raise ExprSyntaxError(f"多出来的「{token.text}」", token.position)
