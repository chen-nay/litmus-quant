"""圈池子 → 逐个算 metric → 一张表（DESIGN.md §1）。

三种形态共用这一步：算出来的表交给 table / card 整形，event_study 走自己的路。

    scope   决定在哪些标的上算 —— Rank 在这个范围里排，「全A等权」也按它算
    subject 决定最后留哪几行

表达式里没有 `Rank` 时，点名看几个就只在那几行上算，不用扫整个池子。
有 `Rank` 就必须在整个池子上算，排名才有意义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import polars as pl

from litmus.data import CONCEPT, STOCK, DataService
from litmus.expr import evaluate, result_unit
from litmus.research.results import Column
from litmus.spec.query import QuerySpec, Scope

#: 只认 Rank(，不认 TsRank(
_RANK = re.compile(r"\bRank\s*\(")


@dataclass(frozen=True)
class Computed:
    """算好的一张表：每行一个标的，每个 metric 一列。"""

    day: date
    #: 算的范围有多少个标的
    pool_size: int
    columns: tuple[Column, ...]
    table: pl.DataFrame  # date, code, <每个 metric 一列>


def pool_of(scope: Scope, day: date, ds: DataService) -> pl.DataFrame:
    """算的范围：(date, code) 两列。"""
    if scope.target != STOCK:
        return ds.get_fields(None, day, day, ["close"], target=scope.target).select("date", "code")
    return ds.get_universe_mask(
        day,
        day,
        base=scope.base,
        industry=scope.industry,
        board=None if scope.board is None else scope.board.model_dump(),
        exclude=list(scope.exclude),
    )


def compute(spec: QuerySpec, ds: DataService) -> Computed:
    day = spec.when.as_of
    if day is None:
        raise ValueError("表和卡要有 when.as_of")
    _require_trading_day(ds, day)

    pool = pool_of(spec.scope, day, ds)
    rows = _rows_to_keep(spec, pool)
    table = rows
    columns = []
    for metric in spec.metrics:
        # 排名要在整个池子里排；其余只在要显示的那几行上算，省一次全池扫描
        on = pool if _RANK.search(metric.expr) else rows
        values = evaluate(metric.expr, "sort", on, day, day, ds, target=spec.scope.target).values
        table = table.join(
            values.select("code", pl.col("value").alias(metric.name)), on="code", how="left"
        )
        columns.append(Column(metric.name, result_unit(metric.expr)))
    return Computed(day=day, pool_size=pool.height, columns=tuple(columns), table=table)


def _rows_to_keep(spec: QuerySpec, pool: pl.DataFrame) -> pl.DataFrame:
    """要显示哪几行。点名的只留点名的那几只，点名里不在池子中的也留着——由检查负责报错。"""
    if spec.subject.kind != "codes":
        return pool
    return pool.filter(pl.col("code").is_in(list(spec.subject.codes)))


def board_notes(spec: QuerySpec, ds: DataService) -> tuple[str, ...]:
    """用概念板块限定股票池时，成分只有快照日那一份。查询日早于快照日就说明白。"""
    board = spec.scope.board
    if board is None or spec.when.as_of is None:
        return ()
    snapshot = ds.concept_snapshot_date()
    if spec.when.as_of >= snapshot:
        return ()
    name = next(
        (item.name for item in ds.list_boards(CONCEPT) if item.code == board.code), board.code
    )
    count = len(ds.board_members(board.code))
    return (
        f"「{name}」按 {snapshot} 的成分（{count} 只）筛选，不是 {spec.when.as_of} 当时的成分："
        "之后才调入的股票也算在内，当时在、后来调出的不会出现",
    )


def _require_trading_day(ds: DataService, day: date) -> None:
    if not ds.get_trading_calendar(day, day):
        raise ValueError(f"{day} 不是交易日")
