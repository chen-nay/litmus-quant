"""把申万行业的三个接口归一。

三种形状完全不同，别套用日频那一套：

- `index_classify`：行业清单，按层级各取一次（申万 2021 版 31 个一级、134 个二级行业）
- `index_member_all`：**带时间区间的归属关系**（`in_date` ~ `out_date`），不是「一行一天」。
  每行同时带一级、二级、三级行业，整理成长表：一行是一只股票在某一级的一段归属。
  `is_new` 是**过滤器**不是开关：`Y` 只给当前成分、`N` 只给已调出的，**两个都要拉**。
  只拉 Y 丢历史（幸存者偏差），只拉 N 丢现在——实测 `is_new='N'` 返回的 2011 行
  全部带 `out_date`，一条当前成分都没有
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

#: 申万分类版本与用到的层级。2026-09-15 加上二级：大家说的「半导体板块」是申万二级行业，一级里只有「电子」
SW_SRC = "SW2021"
SW_LEVELS = ("L1", "L2")

#: 万元 / 万股 → 元 / 股。注意不是 normalize.py 里的千元
TEN_THOUSAND = 10_000

CLASSIFY_FIELDS = "index_code,industry_name,level,industry_code,parent_code,src"
MEMBER_FIELDS = "l1_code,l1_name,l2_code,l2_name,ts_code,in_date,out_date"
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
    """`index_classify`：申万行业清单，一级和二级：(code, name, level, parent_code, src)。一级、二级要一起传进来。

    二级的 parent_code 是上级一级行业的**指数代码**（801010.SI），一级的为空。接口给的 parent_code 是上级的
    **行业代码**（农林牧渔 110000），和清单、归属、日线里用的指数代码对不上，在这里换过来；上级不在清单里的换成空值，
    由同步检查报错。2026-09-15 第一次同步二级时没换，二级全都找不到上级。
    混进来的三级在这里滤掉，免得上层每次都要记得筛一遍。一级在前，同一级按代码排。
    """
    df = frame_from_rows(rows, _schema(CLASSIFY_FIELDS), "index_classify").filter(
        pl.col("level").is_in(SW_LEVELS)
    )
    parents = df.filter(pl.col("level") == "L1").select(
        pl.col("industry_code").alias("parent_code"), pl.col("index_code").alias("_parent")
    )
    return (
        df.join(parents, on="parent_code", how="left")
        .select(
            pl.col("index_code").alias("code"),
            pl.col("industry_name").alias("name"),
            "level",
            pl.when(pl.col("level") == "L1")
            .then(pl.lit(None, dtype=pl.String))
            .otherwise(pl.col("_parent"))
            .alias("parent_code"),
            "src",
        )
        .unique()
        .sort("level", "code")
    )


def normalize_industry_member(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`index_member_all`：股票的申万一级、二级行业归属，**带时间区间**。

    长表：(code, level, industry_code, industry_name, in_date, out_date)，一行是一只股票在某一级（L1 / L2）的一段归属。
    按层级分开存，是因为「某天属于哪个行业」要在同一级里判断（universe.industry_of），两级混在一起就成了同时属于两个行业。

    `out_date` 为空表示至今仍属于这个行业。同一只股票在同一个行业下可能有**多段**区间
    （三级行业调整了、但这一级没变），这里原样保留：判断「某天属于哪个行业」只需要
    「存在一段区间覆盖这天」，不需要把相邻区间合并起来。

    接口按三级行业给区间，只取一级、二级的列之后，三级不同、这一级相同的行会塌成同一行，由 `unique()` 去掉。
    """
    df = frame_from_rows(rows, _schema(MEMBER_FIELDS), "index_member_all")
    per_level = [
        df.select(
            pl.col("ts_code").alias("code"),
            pl.lit(level).alias("level"),
            pl.col(f"{prefix}_code").alias("industry_code"),
            pl.col(f"{prefix}_name").alias("industry_name"),
            _date("in_date").alias("in_date"),
            _date("out_date").alias("out_date"),
        )
        for level, prefix in (("L1", "l1"), ("L2", "l2"))
    ]
    return (
        pl.concat(per_level)
        .filter(pl.col("industry_code").is_not_null())
        .unique()
        .sort("code", "level", "in_date", "industry_code")
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
    "SW_LEVELS",
    "SW_SRC",
    "NormalizeError",
    "normalize_index_classify",
    "normalize_industry_member",
    "normalize_sw_daily",
]
