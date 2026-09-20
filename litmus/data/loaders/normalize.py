"""把 Tushare 的原始返回归一成标准日频面板。

这一层负责三件事，别的都不做：
1. 单位换算：手 → 股、千元 → 元、万元 → 元
2. 字段改名：`ts_code` → `code`、`trade_date` → `date`，其余按 FIELDS 命名
3. 由原始数据推导：后复权价、复权成交量、成交均价、涨跌停标志

面板保持稀疏：只有股票当天真的有成交才有行，停牌日不补齐（见 ARCHITECTURE.md §2.6）。
北交所照常落盘，在股票池那一层排除。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import polars as pl

#: 手 → 股
SHARES_PER_LOT = 100
#: 千元 → 元
THOUSAND_YUAN = 1_000
#: 万元 → 元
TEN_THOUSAND_YUAN = 10_000


class NormalizeError(ValueError):
    """原始数据与预期不符，宁可报错也不往下算。"""


def frame_from_rows(
    rows: Sequence[Mapping], columns: Mapping[str, pl.DataType], source: str
) -> pl.DataFrame:
    """取出需要的列并转成预期类型。缺列、转不了类型都直接报错，不静默补空。

    字符串转成别的类型前，先去掉首尾空白、把空串当空值——这两种都是数据源的正常写法。
    除此之外转不了的值一律报错：宽松转换会把它们变成空值，而且一声不吭。
    2026-09-13 踩过：`tdx_daily.pb` 从 2026 年起带了 10 个前导空格（'          0.82'），
    宽松转换把半年多的市净率全变成了空值，同步照样「成功」。
    """
    if not rows:
        return pl.DataFrame(schema=dict(columns))
    df = pl.DataFrame(list(rows), infer_schema_length=None)
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise NormalizeError(f"{source} 返回缺少字段：{missing}")

    tidied = df.select(_tidy(name, df.schema[name], dtype) for name, dtype in columns.items())
    converted = tidied.select(
        pl.col(name).cast(dtype, strict=False) for name, dtype in columns.items()
    )
    failures = []
    for name in columns:
        lost = converted.get_column(name).is_null() & tidied.get_column(name).is_not_null()
        if lost.any():
            samples = tidied.get_column(name).filter(lost).unique().head(3).to_list()
            failures.append(f"{name} 有 {lost.sum()} 个，如 {samples}")
    if failures:
        raise NormalizeError(f"{source} 有值转不成预期类型：{'；'.join(failures)}")
    return converted


def _tidy(name: str, actual: pl.DataType, expected: pl.DataType) -> pl.Expr:
    """字符串要转成别的类型时，先去首尾空白、空串当空值。字符串列本身原样保留。"""
    column = pl.col(name)
    if actual != pl.String or expected == pl.String:
        return column
    stripped = column.str.strip_chars()
    return pl.when(stripped == "").then(None).otherwise(stripped).alias(name)


def _with_code_and_date(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        pl.col("ts_code").alias("code"),
        pl.col("trade_date").str.to_date("%Y%m%d").alias("date"),
    ).drop("ts_code", "trade_date")


def normalize_daily(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`daily`：未复权行情。vol 单位是手，amount 单位是千元。"""
    df = frame_from_rows(
        rows,
        {
            "ts_code": pl.String,
            "trade_date": pl.String,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "pct_chg": pl.Float64,
            "vol": pl.Float64,
            "amount": pl.Float64,
        },
        "daily",
    )
    return _with_code_and_date(df).select(
        "code",
        "date",
        pl.col("open").alias("open_raw"),
        pl.col("high").alias("high_raw"),
        pl.col("low").alias("low_raw"),
        pl.col("close").alias("close_raw"),
        "pct_chg",
        (pl.col("vol") * SHARES_PER_LOT).alias("volume_raw"),
        (pl.col("amount") * THOUSAND_YUAN).alias("amount"),
    )


def normalize_adj_factor(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`adj_factor`：复权因子。后复权价 = 原始价 × 复权因子。"""
    df = frame_from_rows(
        rows,
        {"ts_code": pl.String, "trade_date": pl.String, "adj_factor": pl.Float64},
        "adj_factor",
    )
    return _with_code_and_date(df).select("code", "date", "adj_factor")


def normalize_daily_basic(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`daily_basic`：每日指标。市值单位是万元。"""
    df = frame_from_rows(
        rows,
        {
            "ts_code": pl.String,
            "trade_date": pl.String,
            "turnover_rate": pl.Float64,
            "pe_ttm": pl.Float64,
            "pb": pl.Float64,
            "ps_ttm": pl.Float64,
            "dv_ttm": pl.Float64,
            "total_mv": pl.Float64,
            "circ_mv": pl.Float64,
        },
        "daily_basic",
    )
    return _with_code_and_date(df).select(
        "code",
        "date",
        pl.col("turnover_rate").alias("turnover"),
        "pe_ttm",
        "pb",
        "ps_ttm",
        "dv_ttm",
        (pl.col("total_mv") * TEN_THOUSAND_YUAN).alias("market_cap"),
        (pl.col("circ_mv") * TEN_THOUSAND_YUAN).alias("circ_mv"),
    )


def normalize_stk_limit(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`stk_limit`：当日涨跌停价（不复权）。"""
    df = frame_from_rows(
        rows,
        {
            "ts_code": pl.String,
            "trade_date": pl.String,
            "up_limit": pl.Float64,
            "down_limit": pl.Float64,
        },
        "stk_limit",
    )
    return _with_code_and_date(df).select("code", "date", "up_limit", "down_limit")


def normalize_stock_st(rows: Sequence[Mapping]) -> pl.DataFrame:
    """`stock_st`：某个交易日处于 ST / *ST 的股票。

    只取 (code, date)——是不是 ST 按天判定，类型名称用不上。
    同一天同一只股票可能返回多行，去重。
    """
    df = frame_from_rows(rows, {"ts_code": pl.String, "trade_date": pl.String}, "stock_st")
    return _with_code_and_date(df).select("code", "date").unique().sort("date", "code")


def _same_price(left: str, right: str) -> pl.Expr:
    """价格相等的判断：在分位（0.01 元）上取整后比较，避免浮点误差。"""
    return (pl.col(left) * 100).round(0) == (pl.col(right) * 100).round(0)


def build_daily_panel(
    daily: pl.DataFrame,
    adj_factor: pl.DataFrame,
    daily_basic: pl.DataFrame,
    stk_limit: pl.DataFrame,
    st: pl.DataFrame,
) -> pl.DataFrame:
    """把五份归一后的数据拼成一天（或一段）的标准面板。

    以 `daily` 为准：它有行才算这只股票当天有成交，停牌日自然就没有行。

    `st` 是 ST 名单，允许覆盖比 daily 更长的区间，join 会按 (code, date) 自己对上。
    它是必传的——传空表可以，省略不行：省略就等于宣称全市场都不是 ST，
    而这正是那种「跑得通、结果全错」的毛病。
    """
    panel = (
        daily.join(adj_factor, on=("code", "date"), how="left")
        .join(daily_basic, on=("code", "date"), how="left")
        .join(stk_limit, on=("code", "date"), how="left")
        .join(
            st.select("code", "date").with_columns(pl.lit(True).alias("is_st")),
            on=("code", "date"),
            how="left",
        )
    )

    missing_adj = panel.select(pl.col("adj_factor").is_null().sum()).item()
    if missing_adj:
        raise NormalizeError(
            f"有 {missing_adj} 行缺复权因子，无法算后复权价；先把 adj_factor 补齐再落盘"
        )

    panel = panel.with_columns(
        (pl.col("open_raw") * pl.col("adj_factor")).alias("open"),
        (pl.col("high_raw") * pl.col("adj_factor")).alias("high"),
        (pl.col("low_raw") * pl.col("adj_factor")).alias("low"),
        (pl.col("close_raw") * pl.col("adj_factor")).alias("close"),
        # 复权成交量：送转后股数会翻倍，除以复权因子才能和历史比较，否则"放量"会被误触发
        (pl.col("volume_raw") / pl.col("adj_factor")).alias("volume"),
        # 成交均价同样取后复权口径，才能和 close 直接比较
        (pl.col("amount") / pl.col("volume_raw") * pl.col("adj_factor")).alias("vwap"),
        _same_price("close_raw", "up_limit").alias("is_limit_up"),
        _same_price("close_raw", "down_limit").alias("is_limit_down"),
        # 内部列：开盘即涨停，用来判断"买不进"
        _same_price("open_raw", "up_limit").alias("open_limit_up"),
        # 没出现在 ST 名单里就是没被 ST，不是「不知道」
        pl.col("is_st").fill_null(False),
    )

    return panel.select(
        "date",
        "code",
        "open",
        "high",
        "low",
        "close",
        "close_raw",
        "volume",
        "amount",
        "vwap",
        "pct_chg",
        "turnover",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "dv_ttm",
        "market_cap",
        "circ_mv",
        "is_st",
        "is_limit_up",
        "is_limit_down",
        "adj_factor",
        "up_limit",
        "down_limit",
        "open_limit_up",
    ).sort("date", "code")
