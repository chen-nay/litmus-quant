"""GET /api/events、GET /api/boards（ARCHITECTURE §6）：确认卡、下拉框要用的清单。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from litmus.api.services import services_of
from litmus.data import BOARD_TARGETS, MissingDataError
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
    """板块清单。type=sw_industry：申万一级行业；type=concept：通达信概念板块，不可用时返回 409 和原因。"""
    if board_type not in BOARD_TARGETS:
        raise HTTPException(status_code=400, detail="type 只能是 sw_industry 或 concept")
    try:
        boards = services_of(request).ds.list_boards(board_type)
    except MissingDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"type": board_type, "boards": [{"code": b.code, "name": b.name} for b in boards]}
