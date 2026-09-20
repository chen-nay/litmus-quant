"""读取时现算的股票字段：财务按公告日对齐、公告日事件、除权除息日、次新股。

这几个字段不落进日频面板：财务和事件每次同步都整张重拉（会补进更正），落进月文件就得回头改
已经走完的月份；次新股的天数是参数。所以落盘的是原始表，读的时候按这里的规则现算。

全是纯函数，输入输出都是 Polars 表，用手写的几行数据就能测（tests/test_derive.py）。
口径与实测依据见 ARCHITECTURE.md §2.6。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import polars as pl

#: 次新股：上市后的前这么多个交易日，含上市当天
NEW_LISTING_DAYS = 60

#: 复权因子比上一条行情涨了超过这个比例，才算除权除息日。2026-09-14 实测复权因子「变小」1.26 万次，
#: 几乎全在 0.05% 以内——数据源的小数位数时三时四，舍入会来回抖；同样幅度的「变大」也是这种噪声。
#: 真实的分红送转在 0.05% 以上：0.05%~0.1% 这一档变大 826 次，变小只有 105 次
EX_DIV_MIN_CHANGE = 5e-4

#: 表达式里的财务字段 → fina_indicator 表的列。$roe 是年化口径（ARCHITECTURE §12）
FINANCE_FIELDS: dict[str, str] = {
    "roe": "roe_yearly",
    "revenue_yoy": "revenue_yoy",
    "profit_yoy": "profit_yoy",
}


class AmbiguousDataError(RuntimeError):
    """本地数据自相矛盾，分不出该取哪条。宁可报错，也不替数据源猜。"""


# ── 财务：按公告日对齐 ──────────────────────────────────────────


def finance_timeline(fina: pl.DataFrame) -> pl.DataFrame:
    """每个公告日起生效的财务值：(code, ann_date, period, roe, revenue_yoy, profit_yoy)。period 是报告期。

    两条规则，都是实测逼出来的：

    1. **同一报告期，标 0 的旧版本只在标 1 的版本出现之前有效**。数据源用 update_flag 标出最新版本：
       同一 (股票, 报告期, 公告日) 常有新旧两行（2026-09-14 实测 4315 组数值不同），偶尔旧版本的
       公告日还比新版本晚一天（26 组）。从没出过新版本的旧版本照常用
    2. **截至某个公告日，取已公告的报告期里最大的那一期；这一期有更正，取最新的一次**。
       不能简单取公告日最新的那条：实测 1.2 万多条旧报告期的记录（补发或更正）是在更新的报告期
       公告之后才发的，那样取会让数值倒退回旧报告期
    """
    flags = set(fina.get_column("update_flag").unique().to_list())
    if not flags <= {"0", "1"}:
        raise AmbiguousDataError(
            f"财务指标的 update_flag 出现了没见过的取值：{sorted(flags - {'0', '1'}, key=str)}"
        )

    fina = fina.filter(pl.col("ann_date").is_not_null())
    first_latest = (
        fina.filter(pl.col("update_flag") == "1")
        .group_by("code", "period")
        .agg(pl.col("ann_date").min().alias("_first_latest"))
    )
    kept = (
        fina.join(first_latest, on=["code", "period"], how="left")
        .filter(
            (pl.col("update_flag") == "1")
            | pl.col("_first_latest").is_null()
            | (pl.col("ann_date") < pl.col("_first_latest"))
        )
        .drop("_first_latest")
    )
    clashes = kept.filter(pl.struct("code", "period", "ann_date").is_duplicated())
    if not clashes.is_empty():
        sample = clashes.select("code", "period", "ann_date").unique().sort("code", "period")
        raise AmbiguousDataError(
            f"财务指标有 {sample.height} 组同一 (股票, 报告期, 公告日) 分不出哪条是最新的，"
            f"如 {sample.head(3).rows()}"
        )

    # 每个公告日：截至当天已公告的最大报告期
    tops = (
        kept.group_by("code", "ann_date")
        .agg(pl.col("period").max())
        .sort("code", "ann_date")
        .with_columns(pl.col("period").cum_max().over("code"))
    )
    columns = list(dict.fromkeys(FINANCE_FIELDS.values()))
    versions = kept.select("code", "period", pl.col("ann_date").alias("_ann"), *columns)
    # 那一期在当天及之前最新的一次公告
    timeline = tops.sort("code", "period", "ann_date").join_asof(
        versions.sort("code", "period", "_ann"),
        left_on="ann_date",
        right_on="_ann",
        by=["code", "period"],
        strategy="backward",
        check_sortedness=False,
    )
    return timeline.select(
        "code",
        "ann_date",
        "period",
        *(pl.col(column).alias(name) for name, column in FINANCE_FIELDS.items()),
    ).sort("code", "ann_date")


def with_finance(rows: pl.DataFrame, timeline: pl.DataFrame) -> pl.DataFrame:
    """给行情行拼上当天已经生效的财务值（公告日 <= 当天）。还没有任何公告的为空值。"""
    return (
        rows.sort("code", "date")
        .join_asof(
            timeline.sort("code", "ann_date"),
            left_on="date",
            right_on="ann_date",
            by="code",
            strategy="backward",
            check_sortedness=False,
        )
        .drop("ann_date", "period")
    )


# ── 公告日事件 ──────────────────────────────────────────────────


def with_event(
    rows: pl.DataFrame,
    events: pl.DataFrame,
    name: str,
    *,
    list_dates: pl.DataFrame,
    since: date,
) -> pl.DataFrame:
    """公告日事件：公告那天该股票有行情就标在当天，否则顺延到它下一个有行情的交易日。

    周末、节假日、停牌期间发的公告，市场要到下一次交易才反应得到。2026-09-14 实测约 27% 的
    财报披露日、29% 的业绩预告日落在该股票没有行情的日子，不顺延就在稀疏面板里直接丢了。
    公告时还没上市的不标（否则上市前发的财报全堆到上市第一天）；早于 since（本地行情起点）的也不标。

    rows：(code, date)。停牌期间的公告要顺延到复牌那天，rows 里就得带上每只股票在查询起点之前的
    最后一行：更早的公告会落在那一行上，由调用方连同那一行一起截掉，不会被顺延进查询区间。
    events：(code, date)，公告日。list_dates：(code, list_date)，查不到上市日的照常标。
    """
    announced = (
        events.select("code", "date")
        .drop_nulls()
        .join(list_dates.select("code", "list_date"), on="code", how="left")
        .filter(
            (pl.col("date") >= since)
            & (pl.col("list_date").is_null() | (pl.col("date") >= pl.col("list_date")))
        )
        .select("code", pl.col("date").alias("_event"))
        .unique()
        .sort("code", "_event")
    )
    trading = rows.select("code", pl.col("date").alias("_day")).unique().sort("code", "_day")
    hits = (
        announced.join_asof(
            trading,
            left_on="_event",
            right_on="_day",
            by="code",
            strategy="forward",
            check_sortedness=False,
        )
        .filter(pl.col("_day").is_not_null())
        .select("code", pl.col("_day").alias("date"))
        .unique()
        .with_columns(pl.lit(True).alias(name))
    )
    return rows.join(hits, on=["code", "date"], how="left").with_columns(
        pl.col(name).fill_null(False)
    )


# ── 除权除息日 ──────────────────────────────────────────────────


def with_ex_div(rows: pl.DataFrame) -> pl.DataFrame:
    """除权除息日：复权因子比该股票上一条行情涨了超过 EX_DIV_MIN_CHANGE。

    停牌期间除权的标在复牌那天。每只股票的第一行没有可比的上一行，不标——所以和公告日事件一样，
    rows 要带上查询起点之前的最后一行。rows：(code, date, adj_factor)。
    """
    previous = pl.col("adj_factor").shift(1).over("code")
    return rows.sort("code", "date").with_columns(
        ((pl.col("adj_factor") / previous - 1) > EX_DIV_MIN_CHANGE)
        .fill_null(False)
        .alias("is_ex_div")
    )


# ── 次新股 ──────────────────────────────────────────────────────


def with_is_new(
    rows: pl.DataFrame,
    list_dates: pl.DataFrame,
    calendar: Sequence[date],
    days: int = NEW_LISTING_DAYS,
    name: str = "is_new",
) -> pl.DataFrame:
    """次新股：上市后的前 days 个交易日，含上市当天。

    按交易日历数，不按这只股票自己的行情数——停牌的日子也算上市的日子。非交易日上市的，
    从下一个交易日算第一天。上市早于交易日历起点的不算次新：数不清它已经上市了多少天，
    而且只影响本地数据最开头的三个月左右。股票列表里查不到上市日的也不算。

    rows：(code, date)，date 是交易日。list_dates：(code, list_date)。
    """
    index = pl.DataFrame({"_day": sorted(calendar)}, schema={"_day": pl.Date}).with_row_index("_i")
    if index.is_empty():
        raise ValueError("交易日历是空的，数不了上市天数")
    listed = (
        list_dates.select("code", "list_date")
        .drop_nulls()
        .filter(pl.col("list_date") >= index.get_column("_day")[0])
        .sort("list_date")
        .join_asof(index, left_on="list_date", right_on="_day", strategy="forward")
        .select("code", pl.col("_i").cast(pl.Int64).alias("_listed"))
    )
    nth_day = pl.col("_i").cast(pl.Int64) - pl.col("_listed") + 1
    return (
        rows.join(index, left_on="date", right_on="_day", how="left")
        .join(listed, on="code", how="left")
        .with_columns((nth_day <= days).fill_null(False).alias(name))
        .drop("_i", "_listed")
    )


def with_list_days(rows: pl.DataFrame, list_dates: pl.DataFrame) -> pl.DataFrame:
    """上市天数：上市以来的自然日天数，上市当天算第 1 天。股票列表里查不到上市日的为空。

    按自然日数，不按交易日：本地交易日历从 2016 年起，更早上市的数不出交易日。
    rows：(code, date)。list_dates：(code, list_date)。
    """
    listed = list_dates.select("code", "list_date").drop_nulls().unique("code")
    days = (pl.col("date") - pl.col("list_date")).dt.total_days() + 1
    return (
        rows.join(listed, on="code", how="left")
        .with_columns(days.cast(pl.Float64).alias("list_days"))
        .drop("list_date")
    )
