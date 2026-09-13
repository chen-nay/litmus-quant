"""把指数的两个接口归一：指数日线、指数历史成分与权重。

两个用途：

- `index_daily`：沪深300 等宽基指数的日线，用作**可选的对照基准**
  （默认基准是当日股票池等权，见 ARCHITECTURE §4.3）
- `index_weight`：指数的**历史**成分与权重，月度快照。用它来还原「2018 年的沪深300
  有哪些股票」——拿今天的成分去回测 2018 年，就是典型的幸存者偏差

**单位注意**：`index_daily` 的 `vol` 是手、`amount` 是千元，和股票日线一样，
但和同一个文件夹里的 `industry.py` 不一样（申万行业日线是万股 / 万元）。
刚写完行业那套再来写这个，最容易顺手抄成 ×10000。

字段以 `api_define/a_share_10_index.md` 为准，只抄不猜。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import polars as pl

from litmus.data.loaders.normalize import (
    SHARES_PER_LOT,
    THOUSAND_YUAN,
    NormalizeError,
    frame_from_rows,
)

#: P0 用到的宽基指数。沪深300 既是可选对照基准，也是一种股票池口径
HS300 = "000300.SH"
ZZ500 = "000905.SH"

INDEX_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pct_chg,vol,amount"
INDEX_WEIGHT_FIELDS = "index_code,con_code,trade_date,weight"

_DAILY_NUMERIC = ("open", "high", "low", "close", "pct_chg", "vol", "amount")


def _schema(fields: str, numeric: Sequence[str] = ()) -> dict[str, pl.DataType]:
    return {name: (pl.Float64 if name in numeric else pl.String) for name in fields.split(",")}


def _date(column: str) -> pl.Expr:
    """YYYYMMDD → 日期。空串与 None 都归成空值。"""
    return pl.col(column).str.to_date("%Y%m%d", strict=False)


def normalize_index_daily(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`index_daily`：指数日线。

    开高低收是**点位**，不是价格：不复权，也不做单位换算。
    成交量和成交额要换：`vol` 是手、`amount` 是千元——和股票日线相同，
    和申万行业日线（万股 / 万元）不同。
    """
    df = frame_from_rows(rows, _schema(INDEX_DAILY_FIELDS, _DAILY_NUMERIC), "index_daily")
    return (
        df.select(
            pl.col("ts_code").alias("code"),
            _date("trade_date").alias("date"),
            "open",
            "high",
            "low",
            "close",
            "pct_chg",
            (pl.col("vol") * SHARES_PER_LOT).alias("volume"),
            (pl.col("amount") * THOUSAND_YUAN).alias("amount"),
        )
        .unique()
        .sort("date", "code")
    )


def normalize_index_weight(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`index_weight`：指数成分与权重，月度快照。

    每个快照日一批成分，权重是百分数（沪深300 全部成分加起来约等于 100）。
    保留**所有**快照日：股票池要按当时那一期的成分算，不能只留最新一期。
    """
    df = frame_from_rows(rows, _schema(INDEX_WEIGHT_FIELDS, ("weight",)), "index_weight")
    return (
        df.select(
            "index_code",
            pl.col("con_code").alias("code"),
            _date("trade_date").alias("date"),
            "weight",
        )
        .unique()
        .sort("index_code", "date", "code")
    )


__all__ = [
    "HS300",
    "INDEX_DAILY_FIELDS",
    "INDEX_WEIGHT_FIELDS",
    "ZZ500",
    "NormalizeError",
    "normalize_index_daily",
    "normalize_index_weight",
]
