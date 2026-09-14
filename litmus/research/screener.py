"""股票表、板块表：某一天筛选 → 排序 → 取前 N（ARCHITECTURE §4.2）。

- 筛选和排序都可以为空：只筛选时按成交额从高到低排；只排序时整个股票池参与排序；两个都没有就是「成交额前 N」
- **排序依据在整个股票池上算**，再取出满足筛选条件的那些：Rank 的范围是「当前查询的股票池」，
  先筛选再排名就成了只在筛选结果里排
- 排序值为空的不进结果（亏损股按市盈率排时没有值），同分按代码排
- 展示列：股票是名称、行业、不复权收盘价、涨跌幅、成交额、市值；板块是名称、收盘点位、涨跌幅、成交额。
  再加上条件和排序里用到的字段
- 用概念板块限定股票池时，概念板块只有快照日的成分（§2.7）。查询日早于快照日，就是拿今天的名单回头看：
  之后才调入的股票算在内，当时在、后来调出的不出现。结果提示里写明按哪天的成分、几只
"""

from __future__ import annotations

from datetime import date

import polars as pl

from litmus.data import CONCEPT, STOCK, DataService
from litmus.expr import collect_fields, evaluate, parse
from litmus.research.results import ListResult
from litmus.spec import BoardListSpec, BoardRef, Condition, Sort, StockListSpec

SORT_VALUE = "sort_value"
DEFAULT_SORT = Sort(by="$amount", order="desc")

_STOCK_COLUMNS = ("close_raw", "pct_chg", "amount", "market_cap")
_BOARD_COLUMNS = ("close", "pct_chg", "amount")


def run_stock_list(spec: StockListSpec, ds: DataService) -> ListResult:
    day = spec.as_of
    _require_trading_day(ds, day)
    universe = spec.universe
    pool = ds.get_universe_mask(
        day,
        day,
        base=universe.base,
        industry=universe.industry,
        board=None if universe.board is None else universe.board.model_dump(),
        exclude=list(universe.exclude),
    )
    notes = _board_notes(universe.board, day, ds)
    return _run("stock_list", day, pool, spec.filter, spec.sort, spec.limit, STOCK, ds, notes)


def run_board_list(spec: BoardListSpec, ds: DataService) -> ListResult:
    day = spec.as_of
    _require_trading_day(ds, day)
    pool = ds.get_fields(None, day, day, ["close"], target=spec.board_type).select("date", "code")
    return _run("board_list", day, pool, spec.filter, spec.sort, spec.limit, spec.board_type, ds)


def _run(
    shape: str,
    day: date,
    pool: pl.DataFrame,
    condition: Condition | None,
    sort: Sort | None,
    limit: int,
    target: str,
    ds: DataService,
    notes: tuple[str, ...] = (),
) -> ListResult:
    notes = list(notes)
    matches = pool
    if condition is not None:
        result = evaluate(condition.expr, "filter", pool, day, day, ds, target=target)
        matches = result.values.filter(pl.col("value")).select("date", "code")

    order = sort or DEFAULT_SORT
    if sort is None:
        notes.append("没有指定排序，按成交额从高到低排")
    scores = evaluate(order.by, "sort", pool, day, day, ds, target=target).values
    ranked = scores.join(matches, on=["date", "code"], how="semi")
    missing = ranked.get_column("value").null_count()
    if missing:
        notes.append(f"有 {missing} 只排序值为空（比如亏损股没有市盈率），没有参与排序")
    top = (
        ranked.drop_nulls("value")
        .sort(["value", "code"], descending=[order.order == "desc", False])
        .head(limit)
        .select("code", pl.col("value").alias(SORT_VALUE))
    )

    used = collect_fields(parse(order.by))
    if condition is not None:
        used |= collect_fields(parse(condition.expr))
    base = _STOCK_COLUMNS if target == STOCK else _BOARD_COLUMNS
    fields = list(dict.fromkeys([*base, *sorted(used)]))
    codes = top.get_column("code").to_list()
    table = top.join(
        ds.get_fields(codes, day, day, fields, target=target).drop("date"), on="code", how="left"
    )
    if target == STOCK:
        info = ds.stock_info(codes, day).select("code", "name", "industry")
        table = table.join(info, on="code", how="left")
        columns = ("code", "name", "industry", SORT_VALUE, *fields)
    else:
        names = pl.DataFrame(
            [(board.code, board.name) for board in ds.list_boards(target)],
            schema={"code": pl.String, "name": pl.String},
            orient="row",
        )
        table = table.join(names, on="code", how="left")
        columns = ("code", "name", SORT_VALUE, *fields)

    ordered = table.select(columns)  # join 不保证顺序，按 top 的名次重排
    position = {code: i for i, code in enumerate(codes)}
    rows = sorted(ordered.iter_rows(named=True), key=lambda row: position[row["code"]])
    return ListResult(shape, day, matches.height, columns, tuple(rows), tuple(notes))


def _board_notes(board: BoardRef | None, day: date, ds: DataService) -> tuple[str, ...]:
    if board is None:
        return ()
    snapshot = ds.concept_snapshot_date()
    if day >= snapshot:
        return ()
    name = next(
        (item.name for item in ds.list_boards(CONCEPT) if item.code == board.code), board.code
    )
    count = len(ds.board_members(board.code))
    return (
        f"「{name}」按 {snapshot} 的成分（{count} 只）筛选，不是 {day} 当时的成分："
        "之后才调入的股票也算在内，当时在、后来调出的不会出现",
    )


def _require_trading_day(ds: DataService, day: date) -> None:
    if not ds.get_trading_calendar(day, day):
        raise ValueError(f"{day} 不是交易日")
