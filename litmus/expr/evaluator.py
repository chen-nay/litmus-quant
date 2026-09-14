"""表达式求值：语法树 → Polars 表达式，在按 (code, date) 排序的长表上算（ARCHITECTURE §3.6）。

这里是纯计算，不读数据：eval_ast 吃进已经取好的长表。取数、预热、截取见 evaluate。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

from litmus.data import STOCK, DataService
from litmus.expr.collector import collect_fields, collect_lookback
from litmus.expr.operators import BOOL
from litmus.expr.operators.cross_section import IN_POOL, rank
from litmus.expr.operators.logic import BINARY, ELEMENTWISE, UNARY
from litmus.expr.operators.timeseries import WINDOWED, cross
from litmus.expr.parser import Binary, Call, Field, Node, Number, Unary, parse
from litmus.expr.validator import PURPOSES, validate

VALUE = "value"
_ROW = "_row"
_REQUIRED = "_required"


def compile_expr(node: Node, ready: dict[int, str] | None = None) -> pl.Expr:
    """语法树 → Polars 表达式。假定已经过校验。

    ready：Rank 节点（按 id）→ 已经算好的排名输入列。按日期分组的排名里不能再嵌按股票分组的窗口，
    Polars 会算错（实测全为空值），所以 eval_ast 先把排名的输入算成列。
    """
    ready = ready or {}
    if isinstance(node, Number):
        # 展开成整列：长度为 1 的常量在分组里平移（Ref、Cross）会变成空值
        return pl.repeat(node.value, pl.len(), dtype=pl.Float64)
    if isinstance(node, Field):
        return pl.col(node.name)
    if isinstance(node, Unary):
        return UNARY[node.op](compile_expr(node.operand, ready))
    if isinstance(node, Binary):
        return BINARY[node.op](compile_expr(node.left, ready), compile_expr(node.right, ready))
    if node.name in WINDOWED:
        subject, window = node.args
        assert isinstance(window, Number)
        return WINDOWED[node.name](compile_expr(subject, ready), int(window.value))
    if node.name == "Rank":
        column = ready.get(id(node))
        return rank(pl.col(column) if column else compile_expr(node.args[0], ready))
    args = [compile_expr(arg, ready) for arg in node.args]
    if node.name == "Cross":
        return cross(*args)
    return ELEMENTWISE[node.name](*args)


def _rank_nodes(node: Node) -> list[Call]:
    """表达式里的 Rank 节点，里层的排在前面。"""
    if isinstance(node, Unary):
        children: tuple[Node, ...] = (node.operand,)
    elif isinstance(node, Binary):
        children = (node.left, node.right)
    elif isinstance(node, Call):
        children = node.args
    else:
        children = ()
    found = [rank_node for child in children for rank_node in _rank_nodes(child)]
    if isinstance(node, Call) and node.name == "Rank":
        found.append(node)
    return found


def eval_ast(node: Node, panel: pl.DataFrame, pool: pl.DataFrame | None = None) -> pl.DataFrame:
    """在长表上算表达式，返回按 (code, date) 排序、多一列 value 的表。

    panel：(date, code, 字段…)，每只标的自己的行情（停牌日没有行）。
    pool：当天在池内的 (date, code)，Rank 只在池内排；None 表示每一行都在池内。
    时序算子照样用标的的全部行，不管那天在不在池内——先删掉池外的行，窗口就断了。
    """
    frame = panel.sort("code", "date")
    if pool is None:
        frame = frame.with_columns(pl.lit(True).alias(IN_POOL))
    else:
        marks = pool.select("date", "code").unique().with_columns(pl.lit(True).alias(IN_POOL))
        frame = (
            frame.join(marks, on=["date", "code"], how="left")
            .with_columns(pl.col(IN_POOL).fill_null(False))
            .sort("code", "date")
        )
    ready: dict[int, str] = {}
    for index, rank_node in enumerate(_rank_nodes(node)):
        column = f"_rank_input_{index}"
        frame = frame.with_columns(compile_expr(rank_node.args[0], ready).alias(column))
        ready[id(rank_node)] = column
    return frame.with_columns(compile_expr(node, ready).alias(VALUE)).drop(list(ready.values()))


# ── 取数、预热、截取 ────────────────────────────────────────────


class ExprDataError(ValueError):
    """扣掉预热期之后，区间里一天都算不出来。"""


@dataclass(frozen=True)
class Evaluation:
    #: (date, code, value)：只含 [start, end] 内、当天在池内的行。筛选和事件的空值已判为不满足（False）
    values: pl.DataFrame
    #: 实际统计起点：扣掉预热期后第一个算得出来的交易日，确认卡和个股回看的统计区间用它。池子为空时是 None
    first_date: date | None
    #: 往前读了多少条行情
    lookback: int


def evaluate(
    expr: str | Node,
    purpose: str,
    universe: pl.DataFrame,
    start: date,
    end: date,
    ds: DataService,
    target: str = STOCK,
) -> Evaluation:
    """算一个表达式（ARCHITECTURE §3.6）。写法或校验不过抛 ExprSyntaxError / ExprValidationError。

    universe：按日股票池 (date, code)。股票用 ds.get_universe_mask()；板块表是当天全部板块。

    - **预热按每只标的自己的行情往前取**，不按交易日历往前推——停过牌的股票才不会凑不够、被悄悄剔掉
    - 时序算子用标的的全部行情算，Rank 只在当天池内排，最后只留池内的行
    - 每只标的在自己的第 required 条行情之前，结果一律置空：窗口没满、EMA 还不准的日子不算数。
      required 一般是完整的预热条数；上市以来行情全在本地的标的（读进来的比要的少、第一条又晚于本地数据起点）
      EMA 从上市第一天算起就是准的，和交易软件一致，不必再等 8n 条
    - start 早于本地数据起点时从起点算；扣掉预热期后一天都算不出来就报错，不给空结果
    """
    if start > end:
        raise ValueError(f"起始日 {start} 晚于结束日 {end}")
    node = parse(expr) if isinstance(expr, str) else expr  # 语法树：比如事件包过 onset 的
    validate(node, target, purpose).raise_if_invalid()
    fields = sorted(collect_fields(node))
    full = collect_lookback(node)
    short = collect_lookback(node, ema_warmup=False)
    coverage_start = ds.data_range(target)[0]
    start = max(start, coverage_start)

    is_condition = PURPOSES[purpose] == BOOL
    pool = universe.select("date", "code").filter(pl.col("date").is_between(start, end))
    if pool.is_empty():
        schema = {
            "date": pl.Date,
            "code": pl.String,
            VALUE: pl.Boolean if is_condition else pl.Float64,
        }
        return Evaluation(pl.DataFrame(schema=schema), None, full)

    codes = pool.get_column("code").unique().to_list()
    panel = ds.get_fields(codes, start, end, fields, target, lookback=full)
    required = (
        panel.group_by("code")
        .agg((pl.col("date") < start).sum().alias("_before"), pl.col("date").min().alias("_first"))
        .select(
            "code",
            pl.when((pl.col("_before") < full) & (pl.col("_first") > coverage_start))
            .then(short)
            .otherwise(full)
            .alias(_REQUIRED),
        )
    )
    frame = (
        eval_ast(node, panel, pool)
        .with_columns(pl.int_range(pl.len()).over("code").alias(_ROW))
        .join(required, on="code")
    )
    warmed = pl.col(_ROW) >= pl.col(_REQUIRED)
    result = frame.with_columns(pl.when(warmed).then(pl.col(VALUE)).alias(VALUE)).filter(
        pl.col("date").is_between(start, end) & pl.col(IN_POOL)
    )
    computable = result.filter(warmed)
    if computable.is_empty():
        raise ExprDataError(
            f"这个表达式要往前读 {full} 条行情，{start} ~ {end} 里一天都算不出来"
            f"（本地数据从 {coverage_start} 起）"
        )
    if is_condition:
        result = result.with_columns(pl.col(VALUE).fill_null(False))
    values = result.select("date", "code", VALUE).sort("date", "code")
    return Evaluation(values, computable.get_column("date").min(), full)
