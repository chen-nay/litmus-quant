"""把 Tushare 的财务与事件数据归一。

**这些表都不进日频面板**，各自单独落盘：

- 财务指标按披露日（PIT）对齐。面板里没有「财务」这些列，是读取时按 `ann_date` 拼上去的。
- 事件（财报披露、业绩预告）是稀疏标记。塞进面板的话，每天 5500 行里
  99% 是 False，白占十年的磁盘；作为独立表存着，读取时 join 成 `$is_report_date` 这些布尔字段。

好处是将来多一类事件只是多一张表，已经落盘的十年面板不用动——面板的列在 ST 那一步
就定死了（见 normalize.py）。

拉取方式：这些接口都按**报告期**取，不按交易日。2016 年至今约 42 个
报告期，每个接口几十次调用，比日频那一万次便宜两个数量级。

字段以 `api_define/` 为准，只抄不猜。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

import polars as pl

from litmus.data.loaders.normalize import NormalizeError, frame_from_rows

logger = logging.getLogger(__name__)

#: 财务指标。输出有上百列，只要这几列——载荷小一个数量级，对慢代理很关键。
#: update_flag 不在默认输出里，要点名才给：2026-09-14 实测同一 (股票, 报告期, 公告日) 有 4315 组
#: 数值不同的行（如 002122.SZ 2023 年报 roe 6.4353 与 6.4077），只有它能区分哪条是最新的
FINA_INDICATOR_FIELDS = "ts_code,ann_date,end_date,roe,roe_yearly,or_yoy,netprofit_yoy,update_flag"
FINA_INDICATOR_NUMERIC = ("roe", "roe_yearly", "or_yoy", "netprofit_yoy")

#: 业绩预告
FORECAST_FIELDS = "ts_code,ann_date,end_date,type,p_change_min,p_change_max"
FORECAST_NUMERIC = ("p_change_min", "p_change_max")

#: 财报披露计划。pre_date 是「预计」披露日，会变；判断财报披露事件必须用 actual_date
DISCLOSURE_FIELDS = "ts_code,ann_date,end_date,pre_date,actual_date"

#: 限售解禁。一个解禁日每个股东一行，按事件聚合是读取时的事
SHARE_FLOAT_FIELDS = "ts_code,ann_date,float_date,float_share,float_ratio,share_type"
SHARE_FLOAT_NUMERIC = ("float_share", "float_ratio")


def _schema(fields: str, numeric: Sequence[str] = ()) -> dict[str, pl.DataType]:
    return {name: (pl.Float64 if name in numeric else pl.String) for name in fields.split(",")}


def _date(column: str) -> pl.Expr:
    """YYYYMMDD → 日期。空串与 None 都归成空值。"""
    return pl.col(column).str.to_date("%Y%m%d", strict=False)


def _warn_missing_ann_date(table: pl.DataFrame, source: str) -> None:
    """没有公告日的记录没法做 PIT 对齐，读取时会被 `ann_date <= 当日` 自然排除。"""
    missing = table.get_column("ann_date").is_null().sum()
    if missing:
        logger.warning("%s 有 %d 行没有公告日，PIT 对齐时用不上", source, missing)


def normalize_fina_indicator(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`fina_indicator` / `fina_indicator_vip`：财务指标，按披露日对齐。

    **不按 (code, period) 去重**：同一个报告期会有更正公告，它们的 `ann_date` 不同，
    是不同的记录。哪一条有效由读取时的 PIT 规则决定（取 `ann_date <= 当日` 里最新的那条），
    在这里去重等于提前替读取方做决定，还会把更正记录抹掉。

    `update_flag` **原样保留成字符串**：同一公告日的几个版本靠它区分，但它的取值在拉到之前
    没亲眼见过，这里不解释、不校验，免得格式不符让整次同步白跑。怎么用由读取时决定。
    """
    df = frame_from_rows(
        rows, _schema(FINA_INDICATOR_FIELDS, FINA_INDICATOR_NUMERIC), "fina_indicator"
    )
    table = (
        df.select(
            pl.col("ts_code").alias("code"),
            _date("ann_date").alias("ann_date"),
            _date("end_date").alias("period"),
            "roe",
            "roe_yearly",
            pl.col("or_yoy").alias("revenue_yoy"),
            pl.col("netprofit_yoy").alias("profit_yoy"),
            "update_flag",
        )
        .unique()  # 整行重复才去掉
        .sort("code", "period", "ann_date")
    )
    _warn_missing_ann_date(table, "fina_indicator")
    return table


def normalize_forecast(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`forecast` / `forecast_vip`：业绩预告。一次修订就是一条新记录，同样不按报告期去重。"""
    df = frame_from_rows(rows, _schema(FORECAST_FIELDS, FORECAST_NUMERIC), "forecast")
    table = (
        df.select(
            pl.col("ts_code").alias("code"),
            _date("ann_date").alias("ann_date"),
            _date("end_date").alias("period"),
            pl.col("type").alias("forecast_type"),
            "p_change_min",
            "p_change_max",
        )
        .unique()
        .sort("code", "period", "ann_date")
    )
    _warn_missing_ann_date(table, "forecast")
    return table


def normalize_disclosure(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`disclosure_date`：财报披露计划。

    `$is_report_date` 只能用 `actual_date`（实际披露日）。`pre_date` 是预计披露日、会被改，
    拿它当事件日就等于用了当时还不知道的信息。

    2026-09-13 实测：19.6 万条两个日期都有的记录里，1152 条（0.6%）实际披露日和预计不符。
    比例不高，但那 1152 次恰恰是"计划赶不上变化"的时刻——推迟披露往往本身就是信号，
    正是最不能算错的那一批。
    """
    df = frame_from_rows(rows, _schema(DISCLOSURE_FIELDS), "disclosure_date")
    return (
        df.select(
            pl.col("ts_code").alias("code"),
            _date("end_date").alias("period"),
            _date("ann_date").alias("ann_date"),
            _date("pre_date").alias("pre_date"),
            _date("actual_date").alias("actual_date"),
        )
        .unique()
        .sort("code", "period")
    )


def normalize_share_float(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`share_float`：限售股解禁。**P0 不同步，留给 P1。**

    同一个解禁日每个股东一行，所以一只股票一天可能有很多行。按事件聚合（解禁总量、
    占总股本比例）是读取时的事，这里保留原始行。

    推迟的原因是数据量（2026-09-13 实测）：单个解禁日 22904 行，一个自然月超过 10 万行——
    多到连一个月都拉不完，因为翻到第 18 页就撞上代理的 offset 上限。全量按天拉要十几个小时，
    而它只换来 `$is_unlock_date` 一个布尔字段。归一逻辑本身是好的、有测试，
    P1 接上 DataSync 即可，见 ARCHITECTURE §11。
    """
    df = frame_from_rows(rows, _schema(SHARE_FLOAT_FIELDS, SHARE_FLOAT_NUMERIC), "share_float")
    return (
        df.select(
            pl.col("ts_code").alias("code"),
            _date("float_date").alias("float_date"),
            _date("ann_date").alias("ann_date"),
            "float_share",
            "float_ratio",
            "share_type",
        )
        .unique()
        .sort("code", "float_date")
    )


__all__ = [
    "DISCLOSURE_FIELDS",
    "FINA_INDICATOR_FIELDS",
    "FORECAST_FIELDS",
    "NormalizeError",
    "SHARE_FLOAT_FIELDS",
    "normalize_disclosure",
    "normalize_fina_indicator",
    "normalize_forecast",
    "normalize_share_float",
]
