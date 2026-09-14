"""data 层：DataService（只读本地 Parquet）与 DataSync（同步、能力探测、manifest）。

其他模块只从这里 import（ARCHITECTURE §1.2 第 3 条），不碰 loaders、storage 这些内部子模块。
"""

from litmus.data.derive import AmbiguousDataError
from litmus.data.fields import (
    BOARD_TARGETS,
    CONCEPT,
    FIELDS,
    INTERNAL_COLUMNS,
    STOCK,
    SW_INDUSTRY,
    Field,
    UnknownFieldError,
    available_targets,
    check_available,
    names_for,
)
from litmus.data.loaders.tushare import TushareError
from litmus.data.service import BoardInfo, DataService
from litmus.data.storage import MarketStore, MissingDataError
from litmus.data.sync import (
    HISTORY_START,
    SYNC_STEPS,
    DataStatus,
    DataSync,
    MonthResult,
    SyncError,
)

__all__ = [
    "BOARD_TARGETS",
    "CONCEPT",
    "FIELDS",
    "HISTORY_START",
    "INTERNAL_COLUMNS",
    "STOCK",
    "SW_INDUSTRY",
    "SYNC_STEPS",
    "AmbiguousDataError",
    "BoardInfo",
    "DataService",
    "DataStatus",
    "DataSync",
    "Field",
    "MarketStore",
    "MissingDataError",
    "MonthResult",
    "SyncError",
    "TushareError",
    "UnknownFieldError",
    "available_targets",
    "check_available",
    "names_for",
]
