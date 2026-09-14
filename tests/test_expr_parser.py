"""表达式解析的测试：优先级、数字写法、报错位置。不读数据。"""

from __future__ import annotations

import pytest

from litmus.expr.parser import (
    MAX_LENGTH,
    Binary,
    Call,
    ExprSyntaxError,
    Field,
    Node,
    Number,
    Unary,
    parse,
)


def shape(node: Node):
    """语法树 → 不带位置的嵌套元组，方便比较结构。"""
    if isinstance(node, Number):
        return node.value
    if isinstance(node, Field):
        return f"${node.name}"
    if isinstance(node, Unary):
        return (node.op, shape(node.operand))
    if isinstance(node, Binary):
        return (node.op, shape(node.left), shape(node.right))
    assert isinstance(node, Call)
    return (node.name, *(shape(arg) for arg in node.args))


def error_of(text: str) -> ExprSyntaxError:
    with pytest.raises(ExprSyntaxError) as info:
        parse(text)
    return info.value


# ── 优先级 ──────────────────────────────────────────────────────


def test_比较比且或优先_和SQL一样():
    """按 Python 的规则会被理解成 $close > (10 & $pe_ttm) < 20，悄悄算错。"""
    assert shape(parse("$close > 10 & $pe_ttm < 20")) == (
        "&",
        (">", "$close", 10.0),
        ("<", "$pe_ttm", 20.0),
    )


def test_且比或优先():
    assert shape(parse("$a > 1 | $b > 2 & $c > 3")) == (
        "|",
        (">", "$a", 1.0),
        ("&", (">", "$b", 2.0), (">", "$c", 3.0)),
    )


def test_乘除比加减优先_算术比比较优先():
    assert shape(parse("$amount > Mean(Ref($amount, 1), 5) * 1.4")) == (
        ">",
        "$amount",
        ("*", ("Mean", ("Ref", "$amount", 1.0), 5.0), 1.4),
    )
    assert shape(parse("($a + $b) * 2")) == ("*", ("+", "$a", "$b"), 2.0)


def test_同一级从左往右():
    assert shape(parse("$a - $b - $c")) == ("-", ("-", "$a", "$b"), "$c")


def test_一元运算最优先():
    assert shape(parse("~$is_st & $close > 10")) == ("&", ("~", "$is_st"), (">", "$close", 10.0))
    assert shape(parse("-$x * 2")) == ("*", ("-", "$x"), 2.0)
    assert shape(parse("~ $a > 1")) == (">", ("~", "$a"), 1.0)  # 类型不对，由校验器拦


def test_加了括号的写法含义不变():
    assert shape(parse("($close > 10) & ($pe_ttm < 20)")) == shape(
        parse("$close > 10 & $pe_ttm < 20")
    )


# ── 数字与调用 ──────────────────────────────────────────────────


def test_数字写法():
    assert shape(parse("$market_cap < 2e10")) == ("<", "$market_cap", 2e10)
    assert shape(parse("$pb < .5")) == ("<", "$pb", 0.5)


def test_区分整数字面量():
    call = parse("Mean($close, 250)")
    assert isinstance(call, Call)
    assert call.args[1].is_int
    assert not parse("250.0").is_int
    assert not parse("2e2").is_int


def test_算子调用与嵌套():
    assert shape(parse("Cross(EMA($close, 12) - EMA($close, 26), 0)")) == (
        "Cross",
        ("-", ("EMA", "$close", 12.0), ("EMA", "$close", 26.0)),
        0.0,
    )
    assert shape(parse("Foo()")) == ("Foo",)  # 没有参数也能解析，算子对不对由校验器管


@pytest.mark.parametrize(
    "text",
    [
        "Cross($close, Mean($close, 250)) & ($amount > Mean(Ref($amount, 1), 20) * 2)",
        "$amount > Mean(Ref($amount, 1), 5) * 1.4",
        "Cross(EMA($close,12) - EMA($close,26), EMA(EMA($close,12) - EMA($close,26), 9))",
        "Rank($profit_yoy) > 0.8",
        "Sum($amount, 5)",
        "($close < Ref($close,1)) & ($amount < Mean(Ref($amount,1),20) * 0.5)",
        "Count($is_limit_up, 2) == 2",
        "$close >= Max($close, 250)",
        "If($is_st, 0, $amount) / Abs(Log($close) - Sign($pct_chg))",
    ],
)
def test_文档里的示例都能解析(text):
    parse(text)


# ── 报错 ────────────────────────────────────────────────────────


def test_比较不能连写():
    error = error_of("1 < $x < 2")
    assert "(x > 1) & (x < 2)" in error.message
    assert error.position == 7


def test_单个等号提示写两个():
    error = error_of("$close = 10")
    assert "==" in error.message and error.position == 7


def test_and_or_not提示换成符号():
    assert "& | ~" in error_of("$a > 1 and $b > 2").message


def test_字段漏了美元符号():
    error = error_of("close > 10")
    assert "$close" in error.message and error.position == 0


def test_全角括号():
    assert "半角" in error_of("Mean（$close, 5)").message


def test_缺右括号():
    error = error_of("Mean($close, 5")
    assert "缺右括号" in error.message and error.position == 14


def test_多出来的东西():
    error = error_of("$close > 10)")
    assert "多出来" in error.message and error.position == 11


def test_没写完():
    assert "没写完" in error_of("$close >").message


def test_空表达式():
    assert "空" in error_of("   ").message


def test_太长():
    assert str(MAX_LENGTH) in error_of("$close + " * 100 + "1").message


def test_报错信息带上从1数起的位置():
    assert str(error_of("$close = 10")).startswith("第 8 个字符")
