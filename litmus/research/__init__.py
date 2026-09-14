"""三种回答的计算：股票表、板块表、个股回看。一个入口 run(spec, ds)，按 spec.shape 分派（ARCHITECTURE §4）。

其他模块只从这里 import（§1.2 第 3 条）。
"""

from litmus.data import DataService
from litmus.research.history import run_stock_history
from litmus.research.results import HistoryResult, HorizonSummary, ListResult, TriggerRecord
from litmus.research.returns import Delay
from litmus.research.screener import run_board_list, run_stock_list
from litmus.spec import BoardListSpec, StockHistorySpec, StockListSpec


def run(
    spec: StockListSpec | BoardListSpec | StockHistorySpec, ds: DataService
) -> ListResult | HistoryResult:
    if isinstance(spec, StockListSpec):
        return run_stock_list(spec, ds)
    if isinstance(spec, BoardListSpec):
        return run_board_list(spec, ds)
    return run_stock_history(spec, ds)


__all__ = [
    "Delay",
    "HistoryResult",
    "HorizonSummary",
    "ListResult",
    "TriggerRecord",
    "run",
]
