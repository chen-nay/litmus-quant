"""POST /api/run、GET /api/run/{run_id}（ARCHITECTURE §1.4 ②、§6）。

- 请求体自己解析：写错了也返回 needs_revision 和中文说明，不用 FastAPI 默认的 422
- 顺序：本地数据够不够（data_not_ready）→ 确定性检查（needs_revision）→ 计算 → 存运行记录
- 计算时才发现数据缺口、预热期不够（MissingDataError、ExprDataError）也是 needs_revision：换个日期或区间就能算
- 其他错误是 failed：把记录编号返回给用户，错误栈打在服务日志里
- 运行记录成功、失败都存，带上数据截至哪天、事件库版本和耗时，事后能对上当时的条件
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from litmus.api.checks import Spec, check_spec
from litmus.api.models import Issue, RunResponse
from litmus.api.serialize import result_to_dict, to_jsonable
from litmus.api.services import Services, services_of
from litmus.data import MissingDataError
from litmus.expr import ExprDataError
from litmus.research import run as run_research
from litmus.spec import StockHistorySpec
from litmus.store import RunRecord

logger = logging.getLogger(__name__)
router = APIRouter()

_BODY_KEYS = ("spec", "plan_id")


@router.post("/api/run")
async def post_run(request: Request) -> RunResponse:
    """body：{"spec": QuerySpec, "plan_id": "..."}，plan_id 可选。"""
    try:
        body = json.loads(await request.body())
    except ValueError:
        return _revise(Issue(message='请求体要是 JSON，如 {"spec": {...}}'))
    if not isinstance(body, dict):
        return _revise(Issue(message='请求体要是一个对象，如 {"spec": {...}}'))
    issues = [Issue(path=str(key), message="不认识这一项") for key in body if key not in _BODY_KEYS]
    if "spec" not in body:
        issues.append(Issue(path="spec", message="缺少这一项"))
    plan_id = body.get("plan_id")
    if plan_id is not None and not isinstance(plan_id, str):
        issues.append(Issue(path="plan_id", message="要是文字"))
    if issues:
        return _revise(*issues)
    # 计算要几秒，放到线程池里，别卡住同时进来的其他请求（比如查同步进度）
    return await run_in_threadpool(execute, body["spec"], plan_id, services_of(request))


def execute(raw_spec: object, plan_id: str | None, services: Services) -> RunResponse:
    status = services.data_status()
    if not status.ready:
        data = {"status": to_jsonable(status), "sync": services.sync_job.snapshot()}
        return RunResponse(status="data_not_ready", message=status.reason, data=data)
    spec, issues = check_spec(raw_spec, services.ds, services.events)
    if spec is None:
        return _revise(*issues)

    started = time.perf_counter()
    try:
        result = run_research(spec, services.ds)
    except (MissingDataError, ExprDataError) as exc:
        return _revise(Issue(message=str(exc)))
    except Exception as exc:  # noqa: BLE001 —— 算不出来也要给用户一个记录编号，不能只回 500
        error = f"{type(exc).__name__}: {exc}"
        run_id = services.store.save_run(
            _record(spec, plan_id, status.data_through, started, status="failed", error=error)
        )
        logger.exception("计算出错，运行记录 %s", run_id)
        message = f"计算时出错了，运行记录编号 {run_id}，详情见服务日志"
        return RunResponse(status="failed", run_id=run_id, message=message)

    payload = result_to_dict(result)
    run_id = services.store.save_run(
        _record(spec, plan_id, status.data_through, started, status="done", result=payload)
    )
    return RunResponse(status="done", run_id=run_id, result=payload)


@router.get("/api/run/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, object]:
    """运行记录：spec、结果或错误、数据截至哪天、事件库版本、耗时。"""
    record = services_of(request).store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"没有编号为 {run_id} 的运行记录")
    return asdict(record)


def _record(
    spec: Spec, plan_id: str | None, data_through: str | None, started: float, **outcome: object
) -> RunRecord:
    return RunRecord(
        spec=spec.model_dump(mode="json", by_alias=True),
        plan_id=plan_id,
        data_through=data_through,
        library_version=spec.event.library_version if isinstance(spec, StockHistorySpec) else None,
        duration_ms=round((time.perf_counter() - started) * 1000),
        **outcome,  # type: ignore[arg-type]
    )


def _revise(*issues: Issue) -> RunResponse:
    return RunResponse(status="needs_revision", issues=list(issues))
