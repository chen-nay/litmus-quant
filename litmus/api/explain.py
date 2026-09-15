"""查询条件 → 确认卡上的说明文字（ARCHITECTURE §5.4）。

模板在 spec.render_assumptions；这里把它要用、spec 自己算不出来的东西查好：表达式的中文（expr.describe）、
最长预热、有没有用排名、股票名、概念板块名和成分快照日、板块数据区间，以及个股回看实际从哪天算
（research.statistics_range，和算结果用的是同一段代码，确认卡上的区间和结果页对得上）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from typing import Any

from litmus.data import CONCEPT, STOCK, SW_INDUSTRY, SW_INDUSTRY_L2, DataService
from litmus.expr import collect_fields, collect_lookback, compares_below, describe, parse
from litmus.research import statistics_range
from litmus.spec import (
    DEFAULTS,
    Assumption,
    BoardListSpec,
    Facts,
    Mention,
    StockHistorySpec,
    StockListSpec,
    render_assumptions,
)
from litmus.store import PlanRecord

Spec = StockListSpec | BoardListSpec | StockHistorySpec

# 只认 Rank(，不认 TsRank(
_RANK = re.compile(r"\bRank\s*\(")
_DIGIT = re.compile(r"\d")

#: 用到这些字段，确认卡上就列出财务数据截至哪天
_FINANCE_FIELDS = frozenset(
    {"roe", "revenue_yoy", "profit_yoy", "is_report_date", "is_forecast_date"}
)


def explain(spec: Spec, ds: DataService, mentions: Sequence[Mention] = ()) -> list[Assumption]:
    """读数据时的缺口、预热期不够（MissingDataError、ExprDataError）照常往外抛，由调用方转成 needs_revision。"""
    return render_assumptions(spec, _facts(spec, ds, tuple(mentions)))


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


#: 比较时只看这一项：股票比代码，表单改过条件后只带代码回来，原话和猜测名会丢
_COMPARED = {"target": "target.code"}


def _industry_scope(name: str, ds: DataService) -> str:
    """股票池限定的申万行业是哪一级：「申万一级行业」「申万二级行业，属于电子」。"""
    if name in {board.name for board in ds.list_boards(SW_INDUSTRY)}:
        return "申万一级行业"
    if SW_INDUSTRY_L2 in ds.available_targets():
        for board in ds.list_boards(SW_INDUSTRY_L2):
            if board.name == name:
                return f"申万二级行业，属于{board.parent}" if board.parent else "申万二级行业"
    return ""


def _industry_mentions(spec: StockListSpec, mentions: tuple[Mention, ...]) -> tuple[Mention, ...]:
    """「半导体板块」大模型记在 universe.board 上；按规则对上的是申万行业时，说明写在行业那一栏。"""
    if spec.universe.board is not None or not spec.universe.industry:
        return mentions
    return tuple(
        Mention(m.phrase, "universe.industry") if m.field == "universe.board" else m
        for m in mentions
    )


def _used_data(spec: Spec, texts: Iterable[str]) -> set[str]:
    """这个问题用到哪几类数据（DataService.latest_dates 的 key）。概念板块成分另有一条说明，这里不列。"""
    fields = set().union(*(collect_fields(parse(text)) for text in texts))
    finance = {"finance"} if fields & _FINANCE_FIELDS else set()
    if isinstance(spec, BoardListSpec):
        # 申万一级、二级的行情在同一张表里，latest_dates 只列一项
        return {
            "sw_industry" if spec.board_type in (SW_INDUSTRY, SW_INDUSTRY_L2) else spec.board_type
        }
    if isinstance(spec, StockHistorySpec):
        return {"stock", *finance, *(("index",) if spec.benchmark.startswith("index:") else ())}
    return {"stock", *finance, *(("index_weight",) if spec.universe.base != "all_a" else ())}


def _data_dates(ds: DataService, used: set[str]) -> tuple[tuple[str, date], ...]:
    """用到的几类数据各自截至哪天（2026-09-15 定：数据旧了不特殊提醒，确认卡上写出来，用户自己决定同步不同步）。"""
    return tuple((item.label, item.day) for item in ds.latest_dates() if item.key in used)


def _defaulted(texts: Mapping[str, str], mentions: Sequence[Mention]) -> frozenset[str]:
    """条件、排序里用了默认门槛、原话里又没给数字的栏目：「小市值」→ 总市值 < 30 亿，确认卡上标默认值。

    原话里自己说了市值门槛（「市值低于 30 亿」）的不算；没有原话（手填、确认卡上改过这一栏）也不算默认值。
    """
    small_cap = float(DEFAULTS["small_cap"])  # type: ignore[arg-type]
    marked = set()
    for path, text in texts.items():
        field = path.split(".")[0]  # filter.expr → filter
        phrases = [m.phrase for m in mentions if m.field.split(".")[0] == field]
        if (
            phrases
            and not any(_gives_cap(phrase) for phrase in phrases)
            and compares_below(parse(text), "market_cap", small_cap)
        ):
            marked.add(field)
    return frozenset(marked)


def _gives_cap(phrase: str) -> bool:
    """原话里自己说了市值门槛：带数字，又说到市值、亿、万（「市值低于 30 亿」「50亿以下」）。

    「市盈率低于 20」带数字但说的不是市值：同一栏里的「小市值」照样按默认门槛标出来。
    """
    return bool(_DIGIT.search(phrase)) and any(word in phrase for word in ("市值", "亿", "万"))


def _at(spec: Mapping[str, Any], path: str) -> object:
    value: object = spec
    for key in path.split("."):
        value = value.get(key) if isinstance(value, Mapping) else None
    return value


def _facts(spec: Spec, ds: DataService, mentions: tuple[Mention, ...]) -> Facts:
    if isinstance(spec, StockHistorySpec):
        code = spec.target.code or ""
        name = ds.stock_info([code], ds.data_range(STOCK)[1]).row(0, named=True)["name"]
        first, _ = statistics_range(spec, ds)
        return Facts(
            stock_name=name,
            first_date=first,
            mentions=mentions,
            data_dates=_data_dates(ds, _used_data(spec, [spec.event.expr])),
        )

    target = STOCK if isinstance(spec, StockListSpec) else spec.board_type
    if isinstance(spec, StockListSpec):
        mentions = _industry_mentions(spec, mentions)
    written = {
        "filter.expr": spec.filter.expr if spec.filter else None,
        "sort.by": spec.sort.by if spec.sort else None,
    }
    texts = {path: text for path, text in written.items() if text}
    facts = Facts(
        expressions={path: describe(text, target) for path, text in texts.items()},
        uses_rank=any(_RANK.search(text) for text in texts.values()),
        lookback=max((collect_lookback(parse(text)) for text in texts.values()), default=0),
        mentions=mentions,
        defaulted=_defaulted(texts, mentions),
        data_dates=_data_dates(ds, _used_data(spec, texts.values())),
    )
    if isinstance(spec, BoardListSpec):
        return replace(facts, board_range=ds.data_range(spec.board_type))
    if spec.universe.industry:
        facts = replace(facts, industry_scope=_industry_scope(spec.universe.industry, ds))
    board = spec.universe.board
    if board is None:
        return facts
    names = {item.code: item.name for item in ds.list_boards(CONCEPT)}
    return replace(
        facts, board_name=names.get(board.code), concept_snapshot=ds.concept_snapshot_date()
    )
