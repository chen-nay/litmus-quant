"""表：筛 → 排 → 取前 N（DESIGN.md §1.5）。

metrics 已经由 compute 算好，这里只做整形：

- 筛选可以为空。排序由 `api.checks` 保证填好——没给排序时它补一个叫「成交额」的指标并指过去，
  这样确认卡上也看得见
- **排序在整个池子上算，再取出满足筛选条件的那些**：`Rank` 的范围是算的范围，
  先筛再排就成了只在筛选结果里排
- 排序值为空的不进结果（亏损股按市盈率排时没有值），同分按代码排。**空的要说清为什么**
  （QUESTIONS.md 翻车模式 #4）：按排序公式用到的字段逐个数，当天没有这个字段的几只、行情不够长的几只
"""

from __future__ import annotations

import polars as pl

from litmus.data import FIELDS, STOCK, DataService
from litmus.expr import collect_fields, collect_lookback, evaluate, parse
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
    missing = matched.filter(pl.col(by).is_null())
    if missing.height:
        expr = next(metric.expr for metric in spec.metrics if metric.name == by)
        reasons = _why_empty(expr, missing.get_column("code").to_list(), computed.day, ds, spec)
        unit = "只" if spec.scope.target == STOCK else "个"
        notes.append(f"有 {missing.height} {unit}排序值为空，没有参与排序：{reasons}")

    descending = output.sort.order == "desc"
    top = (
        matched.drop_nulls(by).sort([by, "code"], descending=[descending, False]).head(output.limit)
    )
    return _to_result(spec, computed, top, matched.height, ds, tuple(notes))


def _why_empty(expr: str, codes: list[str], day, ds: DataService, spec: QuerySpec) -> str:
    """排序值为空的几只为什么空：先按公式用到的字段逐个数当天没有值的，剩下的看行情够不够长。"""
    target = spec.scope.target
    unit = "只" if target == STOCK else "个"
    node = parse(expr)
    fields = [name for name in FIELDS if name in collect_fields(node)]
    today = ds.get_fields(codes, day, day, fields, target)
    left = set(codes)
    parts = []
    for name in fields:
        empty = left - set(today.filter(pl.col(name).is_not_null()).get_column("code").to_list())
        if empty:
            why = "（亏损股没有市盈率）" if name == "pe_ttm" else ""
            parts.append(f"{len(empty)} {unit}当天没有{FIELDS[name].label}{why}")
            left -= empty
    lookback = collect_lookback(node)
    if left and lookback:
        history = ds.get_fields(sorted(left), day, day, fields[:1], target, lookback=lookback)
        rows = dict(history.group_by("code").len().iter_rows())
        short = {code for code in left if rows.get(code, 0) < lookback + 1}
        if short:
            parts.append(f"{len(short)} {unit}行情不到 {lookback + 1} 个交易日，算不出来")
            left -= short
    if left:
        parts.append(f"{len(left)} {unit}算出来不是有限的数（比如除以 0）")
    return "；".join(parts)


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
