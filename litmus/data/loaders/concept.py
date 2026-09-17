"""把通达信概念板块的三个接口归一。

这三个接口都要 **6000 积分**，属于「可选数据」：没权限时能力探测会把它们关掉，
相关字段不进给 LLM 的清单，用户问到概念板块时走「数据不支持」并说明所需积分
（见 ARCHITECTURE §2.5 能力探测）。

**可用区间有限**：实测 `tdx_daily` 的历史只到 2025-03，比股票面板的 2016 年短得多。
这个区间必须显示在确认卡上——否则用户问「最近三年这个概念涨得怎么样」，
拿到的其实只有一年多的数据，而结果看起来完全正常。

单位是这里最容易栽的地方，**同一个接口里就有两种量纲**：

- `tdx_daily.amount` 是**万元**（和申万行业日线一样）
- `tdx_daily.float_mv` 是**亿**（申万行业日线是万元）

另外 `pe` / `pb` 在接口里声明的类型是 `str` 而不是 float，要转成数字才能比较。

字段以 `api_define/a_share_09_limitup.md` 为准，只抄不猜。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import polars as pl

from litmus.data.loaders.normalize import NormalizeError, frame_from_rows

#: 三个接口都要的积分。没权限时能力探测把概念板块关掉，原因里带上这个数
CONCEPT_POINTS = 6000

#: 板块类型。接口一次返回概念 / 行业 / 风格 / 地区四类（实测共 613 个），P0 只要概念板块
CONCEPT_TYPE = "概念板块"

#: 亿 → 元
HUNDRED_MILLION = 100_000_000
#: 万元 → 元
TEN_THOUSAND = 10_000

INDEX_FIELDS = "ts_code,trade_date,name,idx_type,idx_count"
MEMBER_FIELDS = "ts_code,trade_date,con_code,con_name"
DAILY_FIELDS = (
    "ts_code,trade_date,open,high,low,close,pct_change,amount,"
    "turnover_rate,up_num,limit_up_num,pb,float_mv"
)

_DAILY_NUMERIC = (
    "open",
    "high",
    "low",
    "close",
    "pct_change",
    "amount",
    "turnover_rate",
    "up_num",
    "limit_up_num",
    "pb",
    "float_mv",
)


def _schema(fields: str, numeric: Sequence[str] = ()) -> dict[str, pl.DataType]:
    return {name: (pl.Float64 if name in numeric else pl.String) for name in fields.split(",")}


def _date(column: str) -> pl.Expr:
    return pl.col(column).str.to_date("%Y%m%d", strict=False)


def normalize_tdx_index(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`tdx_index`：板块清单。**只留概念板块。**

    接口把概念、行业、风格、地区四类混在一起返回。过滤放在归一层，
    免得「板块排行」把「高贝塔值」这种风格板块混进来当概念讲。
    """
    df = frame_from_rows(rows, _schema(INDEX_FIELDS, ("idx_count",)), "tdx_index")
    return (
        df.filter(pl.col("idx_type") == CONCEPT_TYPE)
        .select(
            pl.col("ts_code").alias("code"),
            "name",
            pl.col("idx_count").alias("member_count"),
            _date("trade_date").alias("date"),
        )
        .unique()
        .sort("code")
    )


def normalize_tdx_member(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`tdx_member`：板块成分。P0 只取当前快照（某个交易日的成分）。

    历史成分推迟到 P1（见 §11）：按交易日循环拉取量太大，回溯范围也还没实测。
    所以这张表回答的是「这个概念**现在**有哪些股票」，不是「当时有哪些」。
    """
    df = frame_from_rows(rows, _schema(MEMBER_FIELDS), "tdx_member")
    return (
        df.select(
            pl.col("ts_code").alias("board_code"),
            pl.col("con_code").alias("code"),
            pl.col("con_name").alias("name"),
            _date("trade_date").alias("date"),
        )
        .unique()
        .sort("board_code", "code")
    )


def normalize_tdx_daily(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`tdx_daily`：板块日线。

    单位在同一个接口里就有两种：`amount` 是万元，`float_mv` 是**亿**。
    `pb` 接口声明为字符串，转成数字。
    """
    df = frame_from_rows(rows, _schema(DAILY_FIELDS, _DAILY_NUMERIC), "tdx_daily")
    return (
        df.select(
            pl.col("ts_code").alias("code"),
            _date("trade_date").alias("date"),
            "open",
            "high",
            "low",
            "close",
            pl.col("pct_change").alias("pct_chg"),
            (pl.col("amount") * TEN_THOUSAND).alias("amount"),
            pl.col("turnover_rate").alias("turnover"),
            "up_num",
            "limit_up_num",
            "pb",
            (pl.col("float_mv") * HUNDRED_MILLION).alias("circ_mv"),
        )
        .unique()
        .sort("date", "code")
    )


__all__ = [
    "CONCEPT_POINTS",
    "CONCEPT_TYPE",
    "DAILY_FIELDS",
    "INDEX_FIELDS",
    "MEMBER_FIELDS",
    "NormalizeError",
    "normalize_tdx_daily",
    "normalize_tdx_index",
    "normalize_tdx_member",
]
