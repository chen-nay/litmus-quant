"""QuerySpec 的定义与校验、默认值表、确认卡说明文字的生成。零内部依赖。

其他模块只从这里 import（ARCHITECTURE §1.2 第 3 条）。
"""

from litmus.spec.defaults import (
    BENCHMARKS,
    DEFAULTS,
    MAX_COST_BPS,
    MAX_HORIZON,
    MAX_HORIZONS,
    MAX_LIMIT,
)
from litmus.spec.query_spec import (
    BoardListSpec,
    BoardRef,
    Condition,
    Event,
    QuerySpec,
    Sort,
    StockHistorySpec,
    StockListSpec,
    Target,
    TimeRange,
    Universe,
    parse_spec,
)

__all__ = [
    "BENCHMARKS",
    "DEFAULTS",
    "MAX_COST_BPS",
    "MAX_HORIZON",
    "MAX_HORIZONS",
    "MAX_LIMIT",
    "BoardListSpec",
    "BoardRef",
    "Condition",
    "Event",
    "QuerySpec",
    "Sort",
    "StockHistorySpec",
    "StockListSpec",
    "Target",
    "TimeRange",
    "Universe",
    "parse_spec",
]
