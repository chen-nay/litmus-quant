"""横截面算子：同一天、在股票池内比较（ARCHITECTURE §3.3）。"""

from __future__ import annotations

import polars as pl

#: 长表上标记「这一行当天在股票池内」的列，由 evaluator 加上
IN_POOL = "_in_pool"


def rank(x: pl.Expr) -> pl.Expr:
    """当天在池内的分位：名次 ÷ 个数，并列取平均，最高为 1。

    不在池内、或值为空的不参与排名，结果也为空。分母是当天池内有值的个数，
    所以 5 只股票的分位依次是 0.2、0.4、0.6、0.8、1.0，`Rank(x) > 0.8` 正好选出最高的那只。
    """
    in_pool = pl.when(pl.col(IN_POOL)).then(x)
    return in_pool.rank("average").over("date") / in_pool.count().over("date")
