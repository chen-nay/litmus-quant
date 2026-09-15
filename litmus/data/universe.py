"""按日股票池：某一天有哪些股票在池内，按当天的状态判断（PIT）。

规则（依据见 ARCHITECTURE.md §2.6）：

- **只从当天有行情的股票里选**：停牌的本来就不在池内；北交所排除
- ST、次新按当天状态剔除
- 沪深300 / 中证500 用当天及之前最近一期成分快照；早于第一期快照直接报错，不给空池子
- 申万行业按当天的归属，一级、二级分开判断（调用方只传那一级的归属表）；概念板块用快照日的当前成分（由调用方传入成分代码）

全是纯函数，表由调用方（DataService）读好传进来。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from datetime import date

import polars as pl

from litmus.data.derive import AmbiguousDataError, with_is_new
from litmus.data.loaders.index import HS300, ZZ500
from litmus.data.storage import MissingDataError

#: 股票池的基础范围 → 取成分用的指数代码。all_a 是沪深 A 股全部，不含北交所
BASES: dict[str, str | None] = {"all_a": None, "hs300": HS300, "zz500": ZZ500}

_NEW_LISTING = re.compile(r"^new_listing_(\d+)d$")


def parse_exclude(exclude: Sequence[str]) -> tuple[bool, int | None]:
    """剔除项 → (是否剔除 ST, 剔除上市不满多少个交易日的次新股)。

    `suspended` 照收，但不用做什么：池子只从当天有行情的股票里选，停牌的本来就不在。
    """
    exclude_st, new_days = False, None
    for token in exclude:
        if token == "ST":
            exclude_st = True
        elif token == "suspended":
            continue
        elif match := _NEW_LISTING.match(token):
            new_days = int(match.group(1))
        else:
            raise ValueError(f"不认识的剔除项 {token!r}，可选 ST、suspended、new_listing_<N>d")
    return exclude_st, new_days


def index_members(days: pl.DataFrame, weights: pl.DataFrame, index_code: str) -> pl.DataFrame:
    """指数在每一天的成分：(date, code)。用当天及之前最近一期快照。

    成分快照有发布滞后（§2.6），按当月去找会拿到空集，所以必须往前找最近一期。
    早于第一期快照的日子找不到任何快照，直接报错——空池子会让上层答出「沪深300里没有满足条件的股票」，
    一个错的结论，还不报错。本地数据从 2016 年起，第一期快照是 2016-01-29。
    """
    snapshots = weights.filter(pl.col("index_code") == index_code)
    if snapshots.is_empty():
        raise MissingDataError(f"本地没有 {index_code} 的成分数据")
    first = snapshots.get_column("date").min()
    too_early = days.filter(pl.col("date") < first)
    if not too_early.is_empty():
        raise MissingDataError(
            f"{index_code} 的成分数据从 {first} 起，{too_early.get_column('date').min()} "
            "早于第一期快照，这一天的股票池无法确定"
        )
    snapshot_days = snapshots.select(pl.col("date").alias("_snapshot")).unique().sort("_snapshot")
    return (
        days.select("date")
        .unique()
        .sort("date")
        .join_asof(snapshot_days, left_on="date", right_on="_snapshot", strategy="backward")
        .join(snapshots.select(pl.col("date").alias("_snapshot"), "code"), on="_snapshot")
        .select("date", "code")
    )


def industry_of(pool: pl.DataFrame, sw_member: pl.DataFrame) -> pl.DataFrame:
    """pool 里每只股票每一天所属的申万行业：(date, code, industry_code)。查不到归属的不出现。

    sw_member 只能是同一级的归属（一级或二级）：两级混在一起，会被当成同一天属于两个行业。

    - 纳入日 <= 当天 <= 剔除日，**剔除日当天还算旧行业**：实测换行业最常见的是新行业的纳入日
      正好是旧行业剔除日的第二天（1542 次）
    - **同一天挂着几条有效归属，取纳入日最新的那条**：实测 63 只股票换了行业、旧归属却没关闭，
      如 000595.SZ 机械设备（1996 起）与公用事业（2026-07-01 起）同时没有剔除日
    - 纳入日也相同就分不出来，报错。实测这样的 3 只股票 2016 年以来都没有行情，碰不到
    """
    active = (
        pool.select("date", "code")
        .join(sw_member.select("code", "industry_code", "in_date", "out_date"), on="code")
        .filter(
            (pl.col("in_date") <= pl.col("date"))
            & (pl.col("out_date").is_null() | (pl.col("date") <= pl.col("out_date")))
        )
    )
    current = active.filter(pl.col("in_date") == pl.col("in_date").max().over("date", "code"))
    ties = (
        current.group_by("date", "code")
        .agg(pl.col("industry_code").n_unique().alias("_n"))
        .filter(pl.col("_n") > 1)
    )
    if not ties.is_empty():
        raise AmbiguousDataError(
            f"申万行业归属有 {ties.height} 个 (日期, 股票) 同一天纳入了不同行业，分不出当天属于哪个，"
            f"如 {ties.sort('date', 'code').select('date', 'code').head(3).rows()}"
        )
    return current.select("date", "code", "industry_code").unique()


def industry_members(
    pool: pl.DataFrame, sw_member: pl.DataFrame, industry_code: str
) -> pl.DataFrame:
    """pool 里每一天属于这个申万行业的股票：(date, code)。归属规则见 industry_of。

    只看曾经进过这个行业的股票：别的股票归属再乱，也影响不到这个行业的成分。
    """
    candidates = sw_member.filter(pl.col("industry_code") == industry_code).select("code").unique()
    in_pool = pool.select("date", "code").join(candidates, on="code", how="semi")
    return (
        industry_of(in_pool, sw_member)
        .filter(pl.col("industry_code") == industry_code)
        .select("date", "code")
    )


def universe_mask(
    traded: pl.DataFrame,
    *,
    base: str = "all_a",
    exclude: Sequence[str] = (),
    industry_code: str | None = None,
    board_codes: Collection[str] | None = None,
    list_dates: pl.DataFrame | None = None,
    calendar: Sequence[date] = (),
    weights: pl.DataFrame | None = None,
    sw_member: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """每天在池内的 (date, code)，按 (date, code) 排序。

    traded：(date, code, is_st)，当天有行情的股票。剔除次新要传 list_dates 与 calendar，
    base 是指数要传 weights，指定行业要传 sw_member。
    """
    if base not in BASES:
        raise ValueError(f"不认识的股票池 {base!r}，可选 {list(BASES)}")
    exclude_st, new_days = parse_exclude(exclude)

    pool = traded.filter(~pl.col("code").str.ends_with(".BJ"))
    if exclude_st:
        pool = pool.filter(~pl.col("is_st").fill_null(False))
    if new_days is not None:
        if list_dates is None:
            raise ValueError("剔除次新股要传 list_dates 与 calendar")
        pool = with_is_new(pool, list_dates, calendar, new_days).filter(~pl.col("is_new"))
    pool = pool.select("date", "code")

    index_code = BASES[base]
    if index_code is not None:
        if weights is None:
            raise ValueError(f"股票池 {base} 要传 weights")
        members = index_members(pool.select("date"), weights, index_code)
        pool = pool.join(members, on=["date", "code"], how="semi")
    if industry_code is not None:
        if sw_member is None:
            raise ValueError("按行业选股要传 sw_member")
        pool = pool.join(
            industry_members(pool, sw_member, industry_code), on=["date", "code"], how="semi"
        )
    if board_codes is not None:
        pool = pool.filter(pl.col("code").is_in(sorted(board_codes)))
    return pool.sort("date", "code")
