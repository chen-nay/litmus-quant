"""研究结果、数据状态 → 能直接写进 JSON 的 dict。store 不认识 research 的类型，转换由 api 负责（ARCHITECTURE §7）。"""

from __future__ import annotations

import math
from dataclasses import fields, is_dataclass
from datetime import date

from litmus.research import HistoryResult, ListResult


def to_jsonable(value: object) -> object:
    """dataclass、NamedTuple 转 dict；日期转 YYYY-MM-DD；dict 的 key 转字符串（持有天数 5 → "5"）；
    NaN、无穷大转 null——JSON 里没有这两个值。"""
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple) and hasattr(value, "_asdict"):
        return to_jsonable(value._asdict())
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def result_to_dict(result: ListResult | HistoryResult) -> dict[str, object]:
    """补上 kind，前端靠它决定怎么展示。"""
    data: dict[str, object] = to_jsonable(result)  # type: ignore[assignment]
    kind = "event_study" if isinstance(result, HistoryResult) else "table"
    return {"kind": kind, **data}
