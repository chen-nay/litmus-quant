"""表达式校验与推导的测试。不读数据。"""

from __future__ import annotations

import pytest

from litmus.expr.collector import collect_fields, collect_lookback
from litmus.expr.parser import parse
from litmus.expr.validator import ExprValidationError, validate

YEAR_LINE = "Cross($close, Mean($close, 250))"
VOLUME_UP = "$amount > Mean(Ref($amount, 1), 20) * 2"
MACD = "Cross(EMA($close,12) - EMA($close,26), EMA(EMA($close,12) - EMA($close,26), 9))"


def messages(text: str, target: str = "stock", purpose: str = "filter") -> list[str]:
    return [issue.message for issue in validate(parse(text), target, purpose).issues]


def one_issue(text: str, target: str = "stock", purpose: str = "filter") -> str:
    found = messages(text, target, purpose)
    assert len(found) == 1, found
    return found[0]


# ── 合法的写法 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "target", "purpose"),
    [
        (f"{YEAR_LINE} & ({VOLUME_UP})", "stock", "filter"),
        ("$amount > Mean(Ref($amount, 1), 5) * 1.4", "stock", "filter"),
        (MACD, "stock", "event"),
        ("Rank($profit_yoy) > 0.8", "stock", "filter"),
        ("$amount > Mean(Ref($amount, 1), 20) * 1.5", "concept", "filter"),
        ("Sum($amount, 5)", "sw_industry", "sort"),
        ("Count($is_limit_up, 2) == 2", "stock", "event"),
        ("$close >= Max($close, 250)", "stock", "event"),
        ("$is_report_date", "stock", "event"),
        ("$close_raw < 10 & ~$is_st", "stock", "filter"),
        ("Rank($close_raw) > 0.5", "stock", "filter"),
        ("Rank(Mean($amount, 20)) > 0.9", "stock", "filter"),
        ("$is_st == $is_new", "stock", "filter"),
        ("If($is_st, 0, $amount) / Abs(Log($close))", "stock", "sort"),
        ("EMA($close, 125) > $close", "stock", "filter"),
        ("PctSince($close, 20251231) > 0.1", "stock", "filter"),
        ("Rank(PctSince($close, 20251231))", "stock", "sort"),
    ],
)
def test_合法的写法通过(text, target, purpose):
    assert messages(text, target, purpose) == []


# ── 字段与算子 ──────────────────────────────────────────────────


def test_没有的字段_大小写写错给提示():
    assert one_issue("$Close > 10") == "没有字段 $Close，是不是 $close？"
    assert "没有字段 $foo" in one_issue("$foo > 10")


def test_内部列不对表达式开放():
    assert "没有字段 $up_limit" in one_issue("$close >= $up_limit")


def test_字段不属于这类标的():
    assert "不能用在申万行业上" in one_issue("$pe_ttm > 10", target="sw_industry")


@pytest.mark.parametrize(
    "text", ["PctSince($close, 2025)", "PctSince($close, 20251340)", "PctSince($close, $open)"]
)
def test_从某天起的日期要写成8位整数(text):
    assert "YYYYMMDD" in one_issue(text, purpose="sort")


def test_找出从某天起的最早起点():
    from datetime import date

    from litmus.expr.collector import collect_since

    text = "Rank(PctSince($close, 20251231)) > 0.9 & PctSince($amount, 20260227) > 0"
    assert collect_since(parse(text)) == date(2025, 12, 31)
    assert collect_since(parse("Pct($close, 5)")) is None


def test_没有的算子_大小写写错给提示():
    assert one_issue("mean($close, 5) > 1") == "没有算子 mean，是不是 Mean？"


def test_参数个数不对():
    assert "要 2 个参数，这里给了 1 个" in one_issue("Mean($close) > 1")


# ── 未来函数与窗口 ──────────────────────────────────────────────


@pytest.mark.parametrize("window", ["-1", "5.5", "$volume", "Ref($close, 1)", "2e1"])
def test_窗口必须是正整数字面量(window):
    assert "引用未来数据" in one_issue(f"Ref($close, {window}) > 1")


def test_窗口的下限与上限():
    assert "至少为 1" in one_issue("Mean($close, 0) > 1")
    assert "至少为 2" in one_issue("Std($close, 1) > 1")
    assert "最大 1000" in one_issue("Mean($close, 1001) > 1")


def test_预热总条数超过上限():
    """收盘价上穿 250 日 EMA：8 × 250 + 1 = 2001 条。"""
    issue = one_issue("Cross($close, EMA($close, 250))")
    assert "2001" in issue and "Mean" in issue
    assert "1249" in one_issue("Mean(Ref($close, 250), 1000) > 1")


# ── 不复权价与排名 ──────────────────────────────────────────────


def test_不复权价不能进时序算子_嵌套也不行():
    assert "不复权价" in one_issue("Mean($close_raw, 5) > 10")
    assert "不复权价" in one_issue("Mean(If($is_st, $close_raw, 0), 5) > 10")
    assert "不复权价" in one_issue("Cross($close_raw, 10)")


def test_Rank不能放进时序算子():
    assert "P0 不支持" in one_issue("Count(Rank($amount) > 0.9, 3) == 3")


def test_事件不能用Rank():
    assert "没有股票池可排" in one_issue("Rank($amount) > 0.9", purpose="event")


# ── 类型 ────────────────────────────────────────────────────────


def test_条件不能求和_提示用Count():
    assert "Count($is_limit_up, n)" in one_issue("Sum($is_limit_up, 5) > 2")


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("$close & $open", "两边都要是条件"),
        ("$is_st + 1 > 0", "两边都要是数值"),
        ("$is_st == 1", "类型要相同"),
        ("~$close > 1", "~ 是对条件取反"),
        ("If($close, 1, 2) > 0", "第一个参数要是条件"),
        ("If($is_st, 1, $is_new)", "两个分支类型要相同"),
        ("Count($close, 5) > 1", "第一个参数要是条件"),
        ("Cross($is_st, 1)", "参数要是数值"),
    ],
)
def test_类型不对(text, hint):
    assert hint in one_issue(text)


def test_结果类型要和用途对上():
    assert "筛选条件要是条件" in one_issue("$close")
    assert "排序依据要是数值" in one_issue("$close > 1", purpose="sort")


def test_指标数值和条件都收():
    """卡上一行「站上年线：是」，所以指标不像排序依据那样必须是数值。"""
    assert validate(parse("$close > Mean($close, 250)"), "stock", "metric").ok
    assert validate(parse("$pe_ttm"), "stock", "metric").ok


def test_没有字段的常量表达式():
    assert "没有任何字段" in one_issue("1 > 0")


def test_一次列出所有问题_带位置():
    result = validate(parse("Mean($foo, 5) > 1 & $bar"), "stock", "filter")
    assert [issue.position for issue in result.issues] == [5, 20]
    with pytest.raises(ExprValidationError, match="第 6 个字符：没有字段 \\$foo"):
        result.raise_if_invalid()


# ── 推导 ────────────────────────────────────────────────────────


def test_推导用到的字段():
    assert collect_fields(parse(f"{YEAR_LINE} & ({VOLUME_UP})")) == {"close", "amount"}


@pytest.mark.parametrize(
    ("text", "lookback"),
    [
        ("$close", 0),
        ("Mean($close, 250)", 249),
        ("Mean(Ref($close, 5), 250)", 254),
        ("Mean($close, 250) > Ref($volume, 5)", 249),
        (YEAR_LINE, 250),
        (VOLUME_UP, 20),
        (MACD, 281),
        (f"({YEAR_LINE}) & ~Ref({YEAR_LINE}, 1)", 251),  # 事件包一层「由不满足变为满足」
        ("Rank(Mean($amount, 20))", 19),
        ("Count($is_limit_up, 2) == 2", 1),
        ("If($is_st, Ref($close, 3), Mean($close, 10))", 9),
        ("Pct($close, 5) + Delta($close, 5)", 5),
        ("TsRank($close, 20) + Std($close, 20)", 19),
        ("EMA($close, 125)", 1000),
    ],
)
def test_推导预热条数(text, lookback):
    assert collect_lookback(parse(text)) == lookback


def test_上市以来行情都在本地时EMA不用额外预热():
    assert collect_lookback(parse(MACD), ema_warmup=False) == 1
    assert collect_lookback(parse("EMA(Mean($close, 20), 12)"), ema_warmup=False) == 19
