"""POST /api/check、POST /api/run、GET /api/run/{run_id}（ARCHITECTURE §1.4 ②、§6）。

- 请求体自己解析：写错了也返回 needs_revision 和中文说明，不用 FastAPI 默认的 422
- /api/check 只检查、不计算：确认卡上改了参数，拿它刷新说明文字（§5.4）
- /api/run 的顺序：本地数据够不够（data_not_ready）→ 确定性检查（needs_revision）→ 生成说明 → 计算 → 存运行记录
- 生成说明、计算时才发现数据缺口、预热期不够（MissingDataError、ExprDataError）也是 needs_revision：换个日期或区间就能算
- 其他错误是 failed：把记录编号返回给用户，错误栈打在服务日志里
- 运行记录成功、失败都存，带上数据截至哪天、事件库版本和耗时；spec 里的默认值标记和说明文字是代码生成的那份
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from litmus.api.checks import Spec, check_spec
from litmus.api.explain import explain
from litmus.api.models import AssumptionItem, CheckResponse, Issue, RunResponse
from litmus.api.serialize import result_to_dict, to_jsonable
from litmus.api.services import Services, services_of
from litmus.data import DataStatus, MissingDataError
from litmus.expr import ExprDataError
from litmus.research import run as run_research
from litmus.spec import Assumption, StockHistorySpec
from litmus.store import RunRecord

logger = logging.getLogger(__name__)
router = APIRouter()

_BODY_KEYS = ("spec", "plan_id")


@router.post("/api/check")
async def post_check(request: Request) -> CheckResponse:
    """body 同 /api/run。通过返回 status=ok、整理好的 spec（事件按事件库生成、默认值标出）和说明文字，不计算。"""
    body, issues = await _read_body(request)
    if body is None:
        return CheckResponse(status="needs_revision", issues=issues)
    return await run_in_threadpool(check, body["spec"], services_of(request))


@router.post("/api/run")
async def post_run(request: Request) -> RunResponse:
    """body：{"spec": QuerySpec, "plan_id": "..."}，plan_id 可选。"""
    body, issues = await _read_body(request)
    if body is None:
        return _revise(*issues)
    # 计算要几秒，放到线程池里，别卡住同时进来的其他请求（比如查同步进度）
    return await run_in_threadpool(execute, body["spec"], body.get("plan_id"), services_of(request))


def check(raw_spec: object, services: Services) -> CheckResponse:
    status = services.data_status()
    if not status.ready:
        return CheckResponse(
            status="data_not_ready", message=status.reason, data=_progress(status, services)
        )
    spec, issues = check_spec(raw_spec, services.ds, services.events)
    if spec is None:
        return CheckResponse(status="needs_revision", issues=issues)
    try:
        spec, assumptions = _explained(spec, services)
    except (MissingDataError, ExprDataError) as exc:
        return CheckResponse(status="needs_revision", issues=[Issue(message=str(exc))])
    return CheckResponse(
        status="ok",
        spec=spec.model_dump(mode="json", by_alias=True),
        assumptions=[
            AssumptionItem(field=item.field, text=item.text, default=item.default)
            for item in assumptions
        ],
    )


def execute(raw_spec: object, plan_id: str | None, services: Services) -> RunResponse:
    status = services.data_status()
    if not status.ready:
        return RunResponse(
            status="data_not_ready", message=status.reason, data=_progress(status, services)
        )
    spec, issues = check_spec(raw_spec, services.ds, services.events)
    if spec is None:
        return _revise(*issues)

    started = time.perf_counter()
    try:
        spec, _ = _explained(spec, services)
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


async def _read_body(request: Request) -> tuple[dict[str, Any] | None, list[Issue]]:
    try:
        body = json.loads(await request.body())
    except ValueError:
        return None, [Issue(message='请求体要是 JSON，如 {"spec": {...}}')]
    if not isinstance(body, dict):
        return None, [Issue(message='请求体要是一个对象，如 {"spec": {...}}')]
    issues = [Issue(path=str(key), message="不认识这一项") for key in body if key not in _BODY_KEYS]
    if "spec" not in body:
        issues.append(Issue(path="spec", message="缺少这一项"))
    plan_id = body.get("plan_id")
    if plan_id is not None and not isinstance(plan_id, str):
        issues.append(Issue(path="plan_id", message="要是文字"))
    return (None, issues) if issues else (body, [])


def _explained(spec: Spec, services: Services) -> tuple[Spec, list[Assumption]]:
    """说明文字写进 spec.assumptions，运行记录里存的就是确认卡上那份。"""
    assumptions = explain(spec, services.ds)
    texts = tuple(item.text for item in assumptions)
    return spec.model_copy(update={"assumptions": texts}), assumptions


def _progress(status: DataStatus, services: Services) -> dict[str, object]:
    return {"status": to_jsonable(status), "sync": services.sync_job.snapshot()}


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
