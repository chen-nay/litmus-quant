"""查询条件 → 确认卡上的说明文字（ARCHITECTURE §5.4）。

模板在 spec.render_assumptions；这里把它要用、spec 自己算不出来的东西查好：表达式的中文（expr.describe）、
最长预热、有没有用排名、股票名、概念板块名和成分快照日、板块数据区间，以及个股回看实际从哪天算
（research.statistics_range，和算结果用的是同一段代码，确认卡上的区间和结果页对得上）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from litmus.data import CONCEPT, STOCK, DataService
from litmus.expr import collect_lookback, describe, parse
from litmus.research import statistics_range
from litmus.spec import (
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
        before = _at(saved, field)
        if before is None or before == _at(spec, field):
            kept.append(Mention(phrase, field))
    return kept


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
        return Facts(stock_name=name, first_date=first, mentions=mentions)

    target = STOCK if isinstance(spec, StockListSpec) else spec.board_type
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
    )
    if isinstance(spec, BoardListSpec):
        return replace(facts, board_range=ds.data_range(spec.board_type))
    board = spec.universe.board
    if board is None:
        return facts
    names = {item.code: item.name for item in ds.list_boards(CONCEPT)}
    return replace(
        facts, board_name=names.get(board.code), concept_snapshot=ds.concept_snapshot_date()
    )
