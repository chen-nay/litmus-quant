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
from litmus.data.service import BoardInfo, DataService
from litmus.data.storage import MissingDataError
from litmus.data.sync import DataStatus, DataSync

__all__ = [
    "BOARD_TARGETS",
    "CONCEPT",
    "FIELDS",
    "INTERNAL_COLUMNS",
    "STOCK",
    "SW_INDUSTRY",
    "AmbiguousDataError",
    "BoardInfo",
    "DataService",
    "DataStatus",
    "DataSync",
    "Field",
    "MissingDataError",
    "UnknownFieldError",
    "available_targets",
    "check_available",
    "names_for",
]
