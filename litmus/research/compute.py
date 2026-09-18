"""圈池子 → 逐个算 metric → 一张表（DESIGN.md §1）。

三种形态共用这一步：算出来的表交给 table / card 整形，event_study 走自己的路。

    scope   决定在哪些标的上算 —— Rank 在这个范围里排，「全A等权」也按它算
    subject 决定最后留哪几行

表达式里没有 `Rank` 时，点名看几个就只在那几行上算，不用扫整个池子。
有 `Rank` 就必须在整个池子上算，排名才有意义，整池的结果留着——卡要数出它排第几。

**点名看的标的不从池子里筛**：当天停牌、是 ST、上市不满 60 个交易日的照样留一行，
值为空，由卡说清为什么（QUESTIONS.md 翻车模式 #4）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import polars as pl

from litmus.data import CONCEPT, STOCK, DataService
from litmus.expr import ExprWarmupError, Field, evaluate, parse, result_unit
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
    #: 算的范围：(date, code)
    pool: pl.DataFrame
    #: 带 Rank 的指标在整个算的范围里的值：指标名 → (date, code, value)
    ranked: Mapping[str, pl.DataFrame]


def pool_of(scope: Scope, day: date, ds: DataService, exclude: bool = True) -> pl.DataFrame:
    """算的范围：(date, code) 两列。exclude=False 只看归属，不剔除 ST、次新。"""
    if scope.target != STOCK:
        return ds.get_fields(None, day, day, ["close"], target=scope.target).select("date", "code")
    return ds.get_universe_mask(
        day,
        day,
        base=scope.base,
        industry=scope.industry,
        board=None if scope.board is None else scope.board.model_dump(),
        exclude=list(scope.exclude) if exclude else [],
    )


def compute(spec: QuerySpec, ds: DataService) -> Computed:
    day = spec.when.as_of
    if day is None:
        raise ValueError("表和卡要有 when.as_of")
    _require_trading_day(ds, day)

    pool = pool_of(spec.scope, day, ds)
    rows = _rows(spec, pool, day)
    named = spec.subject.kind == "codes"
    table = rows
    columns, ranked = [], {}
    for metric in spec.metrics:
        # 排名要在整个池子里排；其余只在要显示的那几行上算，省一次全池扫描
        in_pool = bool(_RANK.search(metric.expr))
        values = _values(metric.expr, pool if in_pool else rows, day, ds, spec, lenient=named)
        if in_pool:
            ranked[metric.name] = values
        table = table.join(
            values.select("code", pl.col("value").alias(metric.name)), on="code", how="left"
        )
        columns.append(Column(metric.name, result_unit(metric.expr), _field(metric.expr)))
    return Computed(
        day=day,
        pool_size=pool.height,
        columns=tuple(columns),
        table=table,
        pool=pool,
        ranked=ranked,
    )


def _field(expr: str) -> str | None:
    node = parse(expr)
    return node.name if isinstance(node, Field) else None


def _values(
    expr: str, on: pl.DataFrame, day: date, ds: DataService, spec: QuerySpec, lenient: bool
) -> pl.DataFrame:
    """一个指标在这些标的上的值。

    点名看的标的行情不够（停牌、还没上市、上市太短）时，预热期一天都算不出来是正常的：
    整列留空，由卡说明原因。表要算一池子，一天都算不出来就是条件本身有问题，照常报错。
    """
    try:
        return evaluate(expr, "metric", on, day, day, ds, target=spec.scope.target).values
    except ExprWarmupError:
        if not lenient:
            raise
        return pl.DataFrame(schema={"date": pl.Date, "code": pl.String, "value": pl.Float64})


def _rows(spec: QuerySpec, pool: pl.DataFrame, day: date) -> pl.DataFrame:
    """要显示哪几行。点名的就是点名那几只，在不在池子里都留着。"""
    if spec.subject.kind != "codes":
        return pool
    return pl.DataFrame(
        {"date": [day] * len(spec.subject.codes), "code": list(spec.subject.codes)},
        schema={"date": pl.Date, "code": pl.String},
    )


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
