"""把 Tushare 的基础数据归一成本地表：股票列表、交易日历、曾用名。

这些表不按天分片，全量拉一次覆盖一张。日频面板的归一见 normalize.py。
字段以 `api_define/a_share_01_basic.md` 为准，只抄不猜。

一个坑：`list_status`、`delist_date`、`pretrade_date` 这类列在接口里是「默认不显示」的，
不显式传 `fields` 就拿不到。所以这里把要的列写成常量，请求和归一共用同一份，
不会出现「请求时忘了要、归一时却在找」的错位。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

import polars as pl

from litmus.data.loaders.normalize import NormalizeError, frame_from_rows

logger = logging.getLogger(__name__)

#: 股票列表要的列。list_status 与 delist_date 默认不返回，必须写在这里
STOCK_BASIC_FIELDS = (
    "ts_code,symbol,name,area,industry,cnspell,market,list_status,list_date,delist_date"
)
#: 交易日历要的列。pretrade_date 用来回答「上一个交易日」
TRADE_CAL_FIELDS = "exchange,cal_date,is_open,pretrade_date"
#: 曾用名要的列
NAMECHANGE_FIELDS = "ts_code,name,start_date,end_date,ann_date,change_reason"

#: 上市状态：L 上市、D 退市、P 暂停上市。三种都要拉，只拉 L 会造成幸存者偏差
LIST_STATUSES = ("L", "D", "P")


def _as_strings(fields: str) -> dict[str, pl.DataType]:
    """接口返回的都当字符串收，日期和布尔在归一时再转。"""
    return {name: pl.String for name in fields.split(",")}


def _date(column: str) -> pl.Expr:
    """YYYYMMDD → 日期。空串与 None 都归成空值——退市日期、当前名的结束日期本来就没有。"""
    return pl.col(column).str.to_date("%Y%m%d", strict=False)


def normalize_stock_basic(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`stock_basic`：股票列表，含已退市与暂停上市。"""
    df = frame_from_rows(rows, _as_strings(STOCK_BASIC_FIELDS), "stock_basic")
    table = df.select(
        pl.col("ts_code").alias("code"),
        "symbol",
        "name",
        "area",
        "industry",
        pl.col("cnspell").alias("pinyin"),
        "market",
        pl.col("list_status").alias("status"),
        _date("list_date").alias("list_date"),
        _date("delist_date").alias("delist_date"),
    ).sort("code")

    # 缺上市日期只影响这只股票的次新股判定，不影响别人，所以记一笔而不是中断整次同步
    missing = table.get_column("list_date").is_null().sum()
    if missing:
        logger.warning("stock_basic 有 %d 只股票没有上市日期，它们不会被标记为次新股", missing)
    return table


def normalize_trade_cal(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`trade_cal`：交易日历。休市日也保留，用来回答某天是不是交易日。"""
    df = frame_from_rows(rows, _as_strings(TRADE_CAL_FIELDS), "trade_cal")
    return df.select(
        _date("cal_date").alias("date"),
        # 代理返回的可能是字符串 "1"，也可能是数字 1，统一按字符串比
        (pl.col("is_open") == "1").alias("is_open"),
        _date("pretrade_date").alias("pretrade_date"),
        "exchange",
    ).sort("date")


def normalize_namechange(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`namechange`：曾用名。`end_date` 为空表示这是现用名。"""
    df = frame_from_rows(rows, _as_strings(NAMECHANGE_FIELDS), "namechange")
    return df.select(
        pl.col("ts_code").alias("code"),
        "name",
        _date("start_date").alias("start_date"),
        _date("end_date").alias("end_date"),
        _date("ann_date").alias("ann_date"),
        "change_reason",
    ).sort("code", "start_date")


__all__ = [
    "LIST_STATUSES",
    "NAMECHANGE_FIELDS",
    "NormalizeError",
    "STOCK_BASIC_FIELDS",
    "TRADE_CAL_FIELDS",
    "normalize_namechange",
    "normalize_stock_basic",
    "normalize_trade_cal",
]
