"""GET /api/data/status、POST /api/data/sync、POST /api/data/sync/stop（ARCHITECTURE §2.5、§6）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from litmus.api.serialize import to_jsonable
from litmus.api.services import services_of

router = APIRouter()


@router.get("/api/data/status")
def data_status(request: Request) -> dict[str, object]:
    """本地数据状态（能不能提问、数据截至、历史补到哪、不可用的可选数据）、各类数据最新到哪天、后台同步进度。
    只读本地，不联网。"""
    services = services_of(request)
    latest = [
        {"key": item.key, "label": item.label, "date": item.day.isoformat()}
        for item in services.ds.latest_dates()
    ]
    return {
        "status": to_jsonable(services.data_status()),
        "latest": latest,
        "sync": services.sync_job.snapshot(),
    }


@router.post("/api/data/sync")
def start_sync(request: Request) -> dict[str, object]:
    """开始后台同步：从 2016 年到今天，缺什么补什么。已经在同步就不再开（started 为 false），照样返回进度。"""
    job = services_of(request).sync_job
    return {"started": job.start(), "sync": job.snapshot()}


@router.post("/api/data/sync/stop")
def stop_sync(request: Request) -> dict[str, object]:
    """停止同步：正在跑的那一步或那个月跑完就停，已经落盘的不受影响，下次同步从断点接着来。"""
    job = services_of(request).sync_job
    return {"stopped": job.stop(), "sync": job.snapshot()}
