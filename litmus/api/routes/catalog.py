"""GET /api/events、GET /api/boards、GET /api/fields（ARCHITECTURE §6）：表单、结果表要用的清单。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from litmus.api.services import services_of
from litmus.data import BOARD_TARGETS, STOCK, MissingDataError
from litmus.expr import field_catalog
from litmus.signals import event_catalog

router = APIRouter()


@router.get("/api/events")
def list_events(request: Request) -> dict[str, object]:
    """事件库：每个事件的名称、参数和可选范围、示例问句。个股回看的 spec 用 preset_id + params 引用。"""
    events = services_of(request).events
    return {"library_version": events.version, "events": event_catalog(events)}


@router.get("/api/boards")
def list_boards(
    request: Request, board_type: Annotated[str | None, Query(alias="type")] = None
) -> dict[str, object]:
    """板块清单和板块数据的可用区间。type=sw_industry：申万一级行业；type=concept：通达信概念板块，
    不可用时返回 409 和原因。"""
    if board_type not in BOARD_TARGETS:
        raise HTTPException(status_code=400, detail="type 只能是 sw_industry 或 concept")
    ds = services_of(request).ds
    try:
        boards = ds.list_boards(board_type)
        first, last = ds.data_range(board_type)
    except MissingDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "type": board_type,
        "range": [first.isoformat(), last.isoformat()],
        "boards": [{"code": b.code, "name": b.name} for b in boards],
    }


@router.get("/api/fields")
def list_fields() -> dict[str, object]:
    """字段清单：中文名、单位、说明，按标的类型分。结果表的列名、表单里的字段参考都用它，和给大模型的是同一份。"""
    return {"fields": {target: field_catalog(target) for target in (STOCK, *BOARD_TARGETS)}}
