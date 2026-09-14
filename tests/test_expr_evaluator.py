"""表达式求值的小表格测试：手写几行输入和手算的答案（ARCHITECTURE §10）。不读数据。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from litmus.expr.evaluator import eval_ast
from litmus.expr.parser import parse

DAY0 = date(2026, 1, 5)


def one_stock(**columns: list) -> pl.DataFrame:
    n = len(next(iter(columns.values())))
    days = [DAY0 + timedelta(days=i) for i in range(n)]
    return pl.DataFrame({"code": ["A"] * n, "date": days, **columns})


def values(text: str, panel: pl.DataFrame, pool: pl.DataFrame | None = None) -> list:
    out = eval_ast(parse(text), panel, pool).sort("code", "date").get_column("value").to_list()
    return [round(v, 6) if isinstance(v, float) else v for v in out]


def on(text: str, **columns: list) -> list:
    return values(text, one_stock(**columns))


# ── 时序算子 ────────────────────────────────────────────────────


def test_均线_文档里的例子():
    assert on("Mean($close, 3)", close=[10.0, 11.0, 12.0, 13.0, 14.0]) == [
        None,
        None,
        11.0,
        12.0,
        13.0,
    ]


def test_求和_最大_最小_样本标准差():
    close = [1.0, 3.0, 2.0]
    assert on("Sum($close, 2)", close=close) == [None, 4.0, 5.0]
    assert on("Max($close, 2)", close=close) == [None, 3.0, 3.0]
    assert on("Min($close, 2)", close=close) == [None, 1.0, 2.0]
    assert on("Std($close, 2)", close=close) == [None, 1.414214, 0.707107]


def test_前值_差值_涨幅():
    close = [10.0, 11.0, 12.1]
    assert on("Ref($close, 1)", close=close) == [None, 10.0, 11.0]
    assert on("Delta($close, 1)", close=close) == [None, 1.0, 1.1]
    assert on("Pct($close, 1)", close=close) == [None, 0.1, 0.1]  # 小数，不是百分数


def test_指数均线从第一条算起():
    """α = 2/(3+1) = 0.5：1 → 0.5·2 + 0.5·1 = 1.5 → 0.5·3 + 0.5·1.5 = 2.25。"""
    assert on("EMA($close, 3)", close=[1.0, 2.0, 3.0]) == [1.0, 1.5, 2.25]


def test_时序分位_并列取平均():
    """窗口 [3,1,2] 里 2 排第 2 → 2/3；窗口 [1,2,2] 里 2 并列第 2、3 名 → 2.5/3。"""
    assert on("TsRank($close, 3)", close=[3.0, 1.0, 2.0, 2.0]) == [None, None, 0.666667, 0.833333]


def test_数条件成立的天数():
    assert on("Count($is_limit_up, 2)", is_limit_up=[True, False, True, True]) == [
        None,
        1.0,
        1.0,
        2.0,
    ]


def test_上穿_和常量比():
    """前一天 <= 2.5 且当天 > 2.5。常量要展开成整列，否则平移后变成空值。

    第一行没有前一天：当天不在上方就确定没上穿（空值 & 假 = 假），当天在上方才是空值。
    """
    close = [1.0, 2.0, 3.0, 2.0, 4.0]
    assert on("Cross($close, 2.5)", close=close) == [False, False, True, False, True]
    assert on("Cross($close, 0.5)", close=[1.0, 2.0]) == [None, False]


def test_上穿_前一天正好相等也算():
    assert on("Cross($close, 2)", close=[2.0, 3.0]) == [False, True]


def test_每只股票各算各的窗口():
    panel = pl.DataFrame(
        {
            "code": ["A", "B", "A", "B", "A"],
            "date": [DAY0, DAY0, DAY0 + timedelta(1), DAY0 + timedelta(1), DAY0 + timedelta(2)],
            "close": [1.0, 100.0, 2.0, 200.0, 3.0],
        }
    )
    assert values("Mean($close, 2)", panel) == [None, 1.5, 2.5, None, 150.0]


def test_窗口里有空值_整段为空():
    assert on("Mean($pe_ttm, 2)", pe_ttm=[1.0, None, 3.0, 5.0]) == [None, None, None, 4.0]


# ── 空值与非有限值 ──────────────────────────────────────────────


def test_空值按三值逻辑传递():
    """市盈率为空的亏损股，~($pe_ttm > 10) 仍是空值，最后判为不满足，不会被选进来。"""
    pe = [5.0, None, 20.0]
    assert on("~($pe_ttm > 10)", pe_ttm=pe) == [True, None, False]
    assert on("($pe_ttm > 10) | $is_st", pe_ttm=pe, is_st=[False, True, False]) == [
        False,
        True,
        True,
    ]
    assert on("($pe_ttm > 10) & $is_st", pe_ttm=pe, is_st=[True, False, True]) == [
        False,
        False,
        True,
    ]


def test_If的条件为空结果也为空():
    assert on("If($pe_ttm > 10, 1, 0)", pe_ttm=[5.0, None, 20.0]) == [0.0, None, 1.0]


def test_除以0与对数的无穷大和NaN变成空值():
    """不处理的话 inf > 2、NaN > 2 在 Polars 里都为真。"""
    amount = [0.0, 5.0, 0.0, 0.0]
    assert on("$amount / Ref($amount, 1) > 2", amount=amount) == [None, None, False, None]
    assert on("Pct($amount, 1)", amount=amount) == [None, None, -1.0, None]
    assert on("Log($x)", x=[-1.0, 0.0, 1.0]) == [None, None, 0.0]


def test_绝对值_符号_负号():
    x = [-2.0, 0.0, 3.0]
    assert on("Abs($x)", x=x) == [2.0, 0.0, 3.0]
    assert on("Sign($x)", x=x) == [-1.0, 0.0, 1.0]
    assert on("-$x * 2", x=x) == [4.0, 0.0, -6.0]


# ── 横截面 ──────────────────────────────────────────────────────


def one_day(**columns: list) -> pl.DataFrame:
    n = len(next(iter(columns.values())))
    return pl.DataFrame({"code": list("ABCDE")[:n], "date": [DAY0] * n, **columns})


def test_排名_名次除以个数_并列取平均_空值不参与():
    panel = one_day(amount=[10.0, 20.0, 20.0, 40.0, None])
    assert values("Rank($amount)", panel) == [0.25, 0.625, 0.625, 1.0, None]


def test_排名只在股票池内():
    panel = one_day(amount=[10.0, 20.0, 30.0, 40.0])
    pool = pl.DataFrame({"date": [DAY0] * 3, "code": ["A", "B", "D"]})
    assert values("Rank($amount)", panel, pool) == [
        pytest.approx(1 / 3),
        pytest.approx(2 / 3),
        None,
        1.0,
    ]


def test_排名里套时序算子_时序先按股票算():
    days = [DAY0, DAY0 + timedelta(1)]
    panel = pl.DataFrame(
        {"code": ["A", "A", "B", "B"], "date": days * 2, "amount": [1.0, 3.0, 10.0, 0.0]}
    )
    # 两天均值：A = 2，B = 5；第一天窗口没满，两只都为空
    assert values("Rank(Mean($amount, 2))", panel) == [None, 0.5, None, 1.0]


def test_不在池内的日子照样参与时序窗口():
    """B 第二天是 ST 不在池内，但算第三天的均线时第二天的成交额照样算进去。"""
    days = [DAY0 + timedelta(i) for i in range(3)]
    panel = pl.DataFrame({"code": ["B"] * 3, "date": days, "amount": [1.0, 2.0, 3.0]})
    pool = pl.DataFrame({"date": [days[0], days[2]], "code": ["B", "B"]})
    assert values("Mean($amount, 3)", panel, pool) == [None, None, 2.0]
