"""逐行计算：算术、比较、与或非、If、Abs、Log、Sign（ARCHITECTURE §3.3）。

两条规则，都是实测 Polars 的行为后定的：

- **空值一路按三值逻辑传递**：空值 & 真 = 空值，空值 | 真 = 真，~空值 = 空值（Polars 默认如此）。
  只在最终的筛选结果上把空值判为不满足，由 evaluator 负责
- **无穷大和 NaN 一律变成空值**：Polars 把 NaN 当成比任何数都大，`0/0 > 2`、`1/0 > 2` 都为真，排名时 NaN 排第一
"""

from __future__ import annotations

from collections.abc import Callable

import polars as pl


def finite(expr: pl.Expr) -> pl.Expr:
    """无穷大、NaN → 空值。"""
    return pl.when(expr.is_finite()).then(expr)


BINARY: dict[str, Callable[[pl.Expr, pl.Expr], pl.Expr]] = {
    "+": lambda a, b: finite(a + b),
    "-": lambda a, b: finite(a - b),
    "*": lambda a, b: finite(a * b),
    "/": lambda a, b: finite(a / b),
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "&": lambda a, b: a & b,
    "|": lambda a, b: a | b,
}

UNARY: dict[str, Callable[[pl.Expr], pl.Expr]] = {
    "-": lambda x: -x,
    "~": lambda x: ~x,
}


def if_(cond: pl.Expr, then: pl.Expr, otherwise: pl.Expr) -> pl.Expr:
    """条件为空时结果也为空——Polars 的 when 会把空值当假，直接用会走到 otherwise。"""
    return pl.when(cond).then(then).when(~cond).then(otherwise)


ELEMENTWISE: dict[str, Callable[..., pl.Expr]] = {
    "If": if_,
    "Abs": lambda x: x.abs(),
    "Log": lambda x: finite(x.log()),
    "Sign": lambda x: x.sign(),
}
