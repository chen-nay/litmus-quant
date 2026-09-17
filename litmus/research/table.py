"""表：筛 → 排 → 取前 N（DESIGN.md §1.5）。

metrics 已经由 compute 算好，这里只做整形：

- 筛选可以为空。排序由 `api.checks` 保证填好——没给排序时它补一个叫「成交额」的指标并指过去，
  这样确认卡上也看得见
- **排序在整个池子上算，再取出满足筛选条件的那些**：`Rank` 的范围是算的范围，
  先筛再排就成了只在筛选结果里排
- 排序值为空的不进结果（亏损股按市盈率排时没有值），同分按代码排
"""

from __future__ import annotations

import polars as pl

from litmus.data import STOCK, DataService
from litmus.expr import evaluate
from litmus.research.compute import Computed, board_notes, compute
from litmus.research.results import ListResult
from litmus.spec.query import QuerySpec, TableOutput

#: 没给排序时按它排
DEFAULT_SORT_NAME = "成交额"
DEFAULT_SORT_EXPR = "$amount"

_NAME_COLUMNS = {STOCK: ("name", "industry")}


def run_table(spec: QuerySpec, ds: DataService) -> ListResult:
    output = spec.output
    assert isinstance(output, TableOutput)
    computed = compute(spec, ds)
    notes = list(board_notes(spec, ds))

    matched = computed.table
    if output.filter is not None:
        hit = evaluate(
            output.filter.expr,
            "filter",
            computed.table.select("date", "code"),
            computed.day,
            computed.day,
            ds,
            target=spec.scope.target,
        ).values
        keep = hit.filter(pl.col("value")).select("date", "code")
        matched = computed.table.join(keep, on=["date", "code"], how="semi")

    if output.sort is None:
        raise ValueError("表要有排序：api.checks 会在没给时补一个「成交额」指标")
    by = output.sort.by
    missing = matched.get_column(by).null_count()
    if missing:
        notes.append(f"有 {missing} 只排序值为空，没有参与排序")

    descending = output.sort.order == "desc"
    top = (
        matched.drop_nulls(by).sort([by, "code"], descending=[descending, False]).head(output.limit)
    )
    return _to_result(spec, computed, top, matched.height, ds, tuple(notes))


def _to_result(
    spec: QuerySpec,
    computed: Computed,
    top: pl.DataFrame,
    total: int,
    ds: DataService,
    notes: tuple[str, ...],
) -> ListResult:
    codes = top.get_column("code").to_list()
    table = top.drop("date")
    if spec.scope.target == STOCK:
        info = ds.stock_info(codes, computed.day).select("code", *_NAME_COLUMNS[STOCK])
        table = table.join(info, on="code", how="left")
        head = ("code", "name", "industry")
    else:
        names = pl.DataFrame(
            [(b.code, b.name) for b in ds.list_boards(spec.scope.target)],
            schema={"code": pl.String, "name": pl.String},
            orient="row",
        )
        table = table.join(names, on="code", how="left")
        head = ("code", "name")

    ordered = table.select(*head, *(c.name for c in computed.columns))
    position = {code: i for i, code in enumerate(codes)}  # join 不保证顺序
    rows = sorted(ordered.iter_rows(named=True), key=lambda row: position[row["code"]])
    return ListResult(
        as_of=computed.day,
        total=total,
        pool_size=computed.pool_size,
        head=head,
        columns=computed.columns,
        rows=tuple(rows),
        notes=notes,
    )
