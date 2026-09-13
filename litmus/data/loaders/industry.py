"""把申万行业的三个接口归一。

三种形状完全不同，别套用日频那一套：

- `index_classify`：行业清单，一次全量取完（申万 2021 版 31 个一级行业）
- `index_member_all`：**带时间区间的归属关系**（`in_date` ~ `out_date`），不是「一行一天」。
  拉的时候必须传 `is_new='N'`，否则只拿到当前成分，历史回测就成了幸存者偏差
- `sw_daily`：行业日线，形状像股票日线，**但单位完全不同**

单位是这里最容易栽的地方。`sw_daily` 的 `vol` 是万股（股票日线是手）、`amount` 是**万元**
（股票日线是千元）、市值也是万元。照搬 normalize.py 的换算会让成交额差十倍。
字段名也差一个词：这里叫 `pct_change`，股票日线叫 `pct_chg`。

字段以 `api_define/a_share_10_index.md` 为准，只抄不猜。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import polars as pl

from litmus.data.loaders.normalize import NormalizeError, frame_from_rows

#: 申万分类版本与层级。P0 只用一级行业
SW_SRC = "SW2021"
SW_LEVEL = "L1"

#: 万元 / 万股 → 元 / 股。注意不是 normalize.py 里的千元
TEN_THOUSAND = 10_000

CLASSIFY_FIELDS = "index_code,industry_name,level,parent_code,src"
MEMBER_FIELDS = "l1_code,l1_name,ts_code,in_date,out_date"
SW_DAILY_FIELDS = (
    "ts_code,trade_date,name,open,high,low,close,pct_change,amount,pb,float_mv,total_mv"
)

_SW_DAILY_NUMERIC = (
    "open",
    "high",
    "low",
    "close",
    "pct_change",
    "amount",
    "pb",
    "float_mv",
    "total_mv",
)


def _schema(fields: str, numeric: Sequence[str] = ()) -> dict[str, pl.DataType]:
    return {name: (pl.Float64 if name in numeric else pl.String) for name in fields.split(",")}


def _date(column: str) -> pl.Expr:
    """YYYYMMDD → 日期。空串与 None 都归成空值。"""
    return pl.col(column).str.to_date("%Y%m%d", strict=False)


def normalize_index_classify(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`index_classify`：申万行业清单。只保留一级行业。

    接口一次能返回 L1/L2/L3，我们只用一级；把过滤放在这里，
    免得上层每次都要记得筛一遍。
    """
    df = frame_from_rows(rows, _schema(CLASSIFY_FIELDS), "index_classify")
    return (
        df.filter(pl.col("level") == SW_LEVEL)
        .select(
            pl.col("index_code").alias("code"),
            pl.col("industry_name").alias("name"),
            "level",
            "src",
        )
        .unique()
        .sort("code")
    )


def normalize_industry_member(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`index_member_all`：股票的申万一级行业归属，**带时间区间**。

    `out_date` 为空表示至今仍属于这个行业。同一只股票在同一个一级行业下可能有**多段**区间
    （三级行业调整了、但一级没变），这里原样保留：判断「某天属于哪个行业」只需要
    「存在一段区间覆盖这天」，不需要把相邻区间合并起来。

    只取一级行业的列，所以三级行业不同、一级相同的那些行会塌成同一行，由 `unique()` 去掉。
    """
    df = frame_from_rows(rows, _schema(MEMBER_FIELDS), "index_member_all")
    return (
        df.select(
            pl.col("ts_code").alias("code"),
            pl.col("l1_code").alias("industry_code"),
            pl.col("l1_name").alias("industry_name"),
            _date("in_date").alias("in_date"),
            _date("out_date").alias("out_date"),
        )
        .unique()
        .sort("code", "in_date", "industry_code")
    )


def normalize_sw_daily(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`sw_daily`：申万行业日线。

    单位与股票日线不同：成交额是**万元**、市值是**万元**（股票那边成交额是千元）。
    涨跌幅字段叫 `pct_change`，不是 `pct_chg`。
    """
    df = frame_from_rows(rows, _schema(SW_DAILY_FIELDS, _SW_DAILY_NUMERIC), "sw_daily")
    return (
        df.select(
            pl.col("ts_code").alias("code"),
            _date("trade_date").alias("date"),
            "name",
            "open",
            "high",
            "low",
            "close",
            pl.col("pct_change").alias("pct_chg"),
            (pl.col("amount") * TEN_THOUSAND).alias("amount"),
            "pb",
            (pl.col("total_mv") * TEN_THOUSAND).alias("market_cap"),
            (pl.col("float_mv") * TEN_THOUSAND).alias("circ_mv"),
        )
        .unique()
        .sort("date", "code")
    )


__all__ = [
    "CLASSIFY_FIELDS",
    "MEMBER_FIELDS",
    "SW_DAILY_FIELDS",
    "SW_LEVEL",
    "SW_SRC",
    "NormalizeError",
    "normalize_index_classify",
    "normalize_industry_member",
    "normalize_sw_daily",
]
