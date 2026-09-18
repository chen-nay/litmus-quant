"""查询条件 → 确认卡的文案（DESIGN.md §1）。

文案怎么拼在 `spec.confirm`；这里把它要用、spec 自己算不出来的东西查好：
每个指标的中文（`expr.describe`）、算的范围有多大、股票和板块的名字、最长预热、
板块数据区间，以及事件统计实际从哪天算（`research.statistics_range`，和算结果用的是同一段代码，
确认卡上的区间和结果页对得上）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from litmus.data import CONCEPT, STOCK, SW_INDUSTRY, SW_INDUSTRY_L2, DataService
from litmus.expr import Call, collect_fields, collect_lookback, compares_below, describe, parse
from litmus.research import pool_of, statistics_range
from litmus.spec import (
    DEFAULTS,
    Assumption,
    CardOutput,
    Confirm,
    EventStudyOutput,
    Facts,
    Mention,
    QuerySpec,
    TableOutput,
    render_confirm,
)
from litmus.store import PlanRecord

# 只认 Rank(，不认 TsRank(
_RANK = re.compile(r"\bRank\s*\(")
_DIGIT = re.compile(r"\d")

#: 用到这些字段，确认卡上就列出财务数据截至哪天
_FINANCE_FIELDS = frozenset(
    {"roe", "revenue_yoy", "profit_yoy", "is_report_date", "is_forecast_date"}
)


def explain(spec: QuerySpec, ds: DataService, mentions: Sequence[Mention] = ()) -> Confirm:
    """读数据时的缺口、预热期不够（MissingDataError、ExprDataError）照常往外抛，由调用方转成 needs_revision。"""
    return render_confirm(spec, _facts(spec, ds, tuple(mentions)))


def assumptions_of(confirm: Confirm) -> list[Assumption]:
    return list(confirm.items)


def plan_mentions(spec: Mapping[str, Any], record: PlanRecord | None) -> list[Mention]:
    """提问时原话里的说法，只留还适用的：确认卡上改过的栏目，不再说「「昨天」理解为……」。

    提问时这一栏还没定下来的（比如要用户从候选里选股票），用户后来选好的照样适用。
    """
    if record is None:
        return []
    saved = record.spec or {}
    kept = []
    for item in record.detail.get("mentions", []):
        phrase, field = item.get("phrase"), item.get("field")
        if not phrase or not field:
            continue
        path = _COMPARED.get(field, field)
        before = _at(saved, path)
        if before is None or before == _at(spec, path):
            kept.append(Mention(phrase, field))
    return kept


#: 比较时只看这一项：点名的标的比代码，表单改过条件后只带代码回来，原话会丢
_COMPARED = {"subject": "subject.codes"}


def _facts(spec: QuerySpec, ds: DataService, mentions: tuple[Mention, ...]) -> Facts:
    texts = {metric.name: metric.expr for metric in spec.metrics}
    filter_expr = (
        spec.output.filter.expr
        if isinstance(spec.output, TableOutput) and spec.output.filter
        else ""
    )
    all_exprs = [*texts.values(), *([filter_expr] if filter_expr else [])]

    common: dict[str, Any] = {
        "mentions": _industry_mentions(spec, mentions),
        "names": _names(spec, ds),
        "data_dates": _data_dates(ds, _used_data(spec, all_exprs)),
    }
    if isinstance(spec.output, EventStudyOutput):
        first, _ = statistics_range(spec, ds)
        return Facts(first_date=first, **common)

    target = spec.scope.target
    card = isinstance(spec.output, CardOutput)
    return Facts(
        metric_texts={name: _metric_text(expr, target, card) for name, expr in texts.items()},
        filter_text=describe(filter_expr, target) if filter_expr else "",
        pool_size=_pool_size(spec, ds),
        industry_scope=_industry_scope(spec.scope.industry, ds),
        board_name=_board_name(spec, ds),
        concept_snapshot=ds.concept_snapshot_date() if spec.scope.board is not None else None,
        board_range=ds.data_range(target) if target != STOCK else None,
        lookback=max((collect_lookback(parse(text)) for text in all_exprs), default=0),
        uses_rank=any(_RANK.search(text) for text in all_exprs),
        defaulted=_defaulted(texts, filter_expr, mentions),
        **common,
    )


def _metric_text(expr: str, target: str, card: bool) -> str:
    """指标的中文。卡上排名写成第几名，说明也照这个说法写。"""
    node = parse(expr)
    if card and isinstance(node, Call) and node.name == "Rank":
        return f"按{describe(node.args[0], target)}，在算的范围里从高到低排名"
    return describe(node, target)


def _industry_mentions(spec: QuerySpec, mentions: tuple[Mention, ...]) -> tuple[Mention, ...]:
    """「半导体板块」大模型记在 scope.board 上；按规则对上的是申万行业时，说明写在行业那一行。"""
    if spec.scope.board is not None or not spec.scope.industry:
        return mentions
    return tuple(
        Mention(m.phrase, "scope.industry") if m.field == "scope.board" else m for m in mentions
    )


def _pool_size(spec: QuerySpec, ds: DataService) -> int | None:
    if spec.when.as_of is None:
        return None
    return pool_of(spec.scope, spec.when.as_of, ds).height


def _names(spec: QuerySpec, ds: DataService) -> dict[str, str]:
    """点名看的标的叫什么。"""
    codes = list(spec.subject.codes)
    if not codes:
        return {}
    if spec.scope.target != STOCK:
        return {b.code: b.name for b in ds.list_boards(spec.scope.target) if b.code in set(codes)}
    day = spec.when.as_of or (spec.when.range.end if spec.when.range else ds.data_range(STOCK)[1])
    info = ds.stock_info(codes, day)
    return dict(zip(info.get_column("code"), info.get_column("name"), strict=True))


def _board_name(spec: QuerySpec, ds: DataService) -> str | None:
    board = spec.scope.board
    if board is None:
        return None
    return next((item.name for item in ds.list_boards(CONCEPT) if item.code == board.code), None)


def _industry_scope(name: str | None, ds: DataService) -> str:
    """算的范围限定的申万行业是哪一级：「申万一级行业」「申万二级行业，属于电子」。"""
    if not name:
        return ""
    if name in {board.name for board in ds.list_boards(SW_INDUSTRY)}:
        return "申万一级行业"
    if SW_INDUSTRY_L2 in ds.available_targets():
        for board in ds.list_boards(SW_INDUSTRY_L2):
            if board.name == name:
                return f"申万二级行业，属于{board.parent}" if board.parent else "申万二级行业"
    return ""


def _used_data(spec: QuerySpec, texts: Iterable[str]) -> set[str]:
    """这个问题用到哪几类数据（DataService.latest_dates 的 key）。概念板块成分另有一条说明，这里不列。"""
    exprs = list(texts)
    fields = set().union(*(collect_fields(parse(text)) for text in exprs)) if exprs else set()
    finance = {"finance"} if fields & _FINANCE_FIELDS else set()
    if isinstance(spec.output, EventStudyOutput):
        return {
            "stock",
            *finance,
            *(("index",) if spec.output.benchmark.startswith("index:") else ()),
        }
    if spec.scope.target != STOCK:
        # 申万一级、二级的行情在同一张表里，latest_dates 只列一项
        key = (
            "sw_industry"
            if spec.scope.target in (SW_INDUSTRY, SW_INDUSTRY_L2)
            else spec.scope.target
        )
        return {key}
    return {"stock", *finance, *(("index_weight",) if spec.scope.base != "all_a" else ())}


def _data_dates(ds: DataService, used: set[str]) -> tuple[tuple[str, date], ...]:
    """用到的几类数据各自截至哪天。数据旧了不特殊提醒，确认卡上写出来，用户自己决定同步不同步。"""
    return tuple((item.label, item.day) for item in ds.latest_dates() if item.key in used)


def _defaulted(
    texts: Mapping[str, str], filter_expr: str, mentions: Sequence[Mention]
) -> frozenset[str]:
    """用了默认门槛、原话里又没给数字的栏目：「小市值」→ 总市值 < 30 亿，确认卡上标默认值。

    原话里自己说了市值门槛（「市值低于 30 亿」）的不算。
    """
    small_cap = float(DEFAULTS["small_cap"])  # type: ignore[arg-type]
    paths = {f"metrics.{name}": expr for name, expr in texts.items()}
    if filter_expr:
        paths["output.filter"] = filter_expr
    marked = set()
    for path, text in paths.items():
        phrases = [m.phrase for m in mentions if _under(m.field, path)]
        if (
            phrases
            and not any(_gives_cap(phrase) for phrase in phrases)
            and compares_below(parse(text), "market_cap", small_cap)
        ):
            marked.add(path)
    return frozenset(marked)


def _gives_cap(phrase: str) -> bool:
    """原话里自己说了市值门槛：带数字，又说到市值、亿、万（「市值低于 30 亿」「50亿以下」）。"""
    return bool(_DIGIT.search(phrase)) and any(word in phrase for word in ("市值", "亿", "万"))


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}.")


def _at(spec: Mapping[str, Any], path: str) -> object:
    value: object = spec
    for key in path.split("."):
        value = value.get(key) if isinstance(value, Mapping) else None
    return value
