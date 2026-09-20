"""三种形态的计算：表、卡、统计。一个入口 run(spec, ds)，按 output.kind 分派（DESIGN.md §1.5）。

其他模块只从这里 import（ARCHITECTURE §1.2 第 3 条）。
"""

from litmus.data import DataService
from litmus.research.card import run_card
from litmus.research.compute import Computed, compute, pool_of
from litmus.research.history import run_event_study, statistics_range
from litmus.research.results import (
    CardItem,
    CardResult,
    CardRow,
    Column,
    HistoryResult,
    HorizonSummary,
    ListResult,
    TriggerRecord,
)
from litmus.research.returns import Delay
from litmus.research.table import run_table
from litmus.spec.query import QuerySpec


def run(spec: QuerySpec, ds: DataService) -> ListResult | CardResult | HistoryResult:
    if spec.output.kind == "event_study":
        return run_event_study(spec, ds)
    if spec.output.kind == "card":
        return run_card(spec, ds)
    return run_table(spec, ds)


__all__ = [
    "CardItem",
    "CardResult",
    "CardRow",
    "Column",
    "Computed",
    "Delay",
    "HistoryResult",
    "HorizonSummary",
    "ListResult",
    "TriggerRecord",
    "compute",
    "pool_of",
    "run",
    "run_card",
    "statistics_range",
]
