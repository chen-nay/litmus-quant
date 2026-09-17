"""时序算子：沿时间轴、逐标的计算（ARCHITECTURE §3.3）。

所有窗口都在按 (code, date) 排好序的长表上、按 code 分组算。面板是稀疏的，停牌日没有行，
所以「过去 n 个交易日」数的是该标的自己有行情的日子——长期停牌股复牌当天不会把停牌期间的成交额按 0 算进窗口。

窗口没满 n 条、或窗口里有空值，结果为空值，不猜（Polars 的默认行为）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import polars as pl

from litmus.expr.operators.logic import finite

BY = "code"


def ref(x: pl.Expr, n: int) -> pl.Expr:
    return x.shift(n).over(BY)


def pct(x: pl.Expr, n: int) -> pl.Expr:
    return finite(x / ref(x, n) - 1)


def pct_since(x: pl.Expr, day: date) -> pl.Expr:
    """x 较 day 那天的涨跌幅：按日期取值，不按条数——那天停牌就用它之前最后一个交易日的值。

    day 当天及以前为空，不拿之后的数据当起点。那天之前没有行情（之后才上市）的也为空。
    """
    base = x.filter(pl.col("date") <= day).last().over(BY)
    return pl.when(pl.col("date") > day).then(finite(x / base - 1))


def cross(x: pl.Expr, y: pl.Expr) -> pl.Expr:
    """上穿：前一个交易日 x <= y，当天 x > y。"""
    return (ref(x, 1) <= ref(y, 1)) & (x > y)


WINDOWED: dict[str, Callable[[pl.Expr, int], pl.Expr]] = {
    "Mean": lambda x, n: x.rolling_mean(n).over(BY),
    # adjust=False：y[t] = α·x[t] + (1-α)·y[t-1]，α = 2/(n+1)，和交易软件一致
    "EMA": lambda x, n: x.ewm_mean(span=n, adjust=False).over(BY),
    "Std": lambda x, n: x.rolling_std(n).over(BY),  # 样本标准差
    "Sum": lambda x, n: x.rolling_sum(n).over(BY),
    "Max": lambda x, n: x.rolling_max(n).over(BY),
    "Min": lambda x, n: x.rolling_min(n).over(BY),
    "Ref": ref,
    "Delta": lambda x, n: x - ref(x, n),
    "Pct": pct,
    # 当天的值在窗口里的名次 ÷ n，并列取平均，最高为 1
    "TsRank": lambda x, n: x.rolling_rank(n, method="average").over(BY) / n,
    "Count": lambda cond, n: cond.cast(pl.Float64).rolling_sum(n).over(BY),
}
