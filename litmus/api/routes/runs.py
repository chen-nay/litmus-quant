"""POST /api/check、POST /api/run、GET /api/run/{run_id}（ARCHITECTURE §1.4 ②、§6）。

- 请求体自己解析：写错了也返回 needs_revision 和中文说明，不用 FastAPI 默认的 422
- /api/check 只检查、不计算：确认卡上改了参数，拿它刷新说明文字（§5.4）
- /api/run 的顺序：本地数据够不够（data_not_ready）→ 确定性检查（needs_revision）→ 生成说明 → 计算 → 存运行记录
- 生成说明、计算时才发现数据缺口、预热期不够（MissingDataError、ExprDataError）也是 needs_revision：换个日期或区间就能算
- 其他错误是 failed：把记录编号返回给用户，错误栈打在服务日志里
- 运行记录成功、失败都存，带上数据截至哪天、事件库版本和耗时；spec 里的默认值标记和说明文字是代码生成的那份
- 每一步也记进过程记录（store 的 `traces/tr<run_id>.json`，2026-09-16 加）：检查、生成说明、计算各花了多久，
  结果多大，出错是哪一步出的。这几步是纯代码，同样的条件和数据算出来一样，所以只记耗时和规模；条件和结果在运行记录里。
  卡的小结是大模型写的，写的时候把每次调用接在这条过程记录后面（`llm.narrate`）。
  **记录失败不影响回答**。检查没过（needs_revision）不记——那些问题本来就原样显示给用户了，不是黑箱
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import asdict, replace
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from litmus.api.checks import Spec, check_spec
from litmus.api.explain import explain, plan_mentions
from litmus.api.models import AssumptionItem, CheckResponse, Issue, NarrativeResponse, RunResponse
from litmus.api.serialize import result_to_dict, to_jsonable
from litmus.api.services import Services, services_of
from litmus.data import DataStatus, MissingDataError
from litmus.expr import ExprDataError
from litmus.llm import LLMCall, narrate
from litmus.research import run as run_research
from litmus.spec import Confirm, EventStudyOutput
from litmus.store import NarrativeRecord, RunRecord, TraceRecord

logger = logging.getLogger(__name__)
router = APIRouter()

_BODY_KEYS = ("spec", "plan_id")


@router.post("/api/check")
async def post_check(request: Request) -> CheckResponse:
    """body 同 /api/run。通过返回 status=ok、整理好的 spec（事件按事件库生成、默认值标出）和说明文字，不计算。"""
    body, issues = await _read_body(request)
    if body is None:
        return CheckResponse(status="needs_revision", issues=issues)
    return await run_in_threadpool(check, body["spec"], body.get("plan_id"), services_of(request))


@router.post("/api/run")
async def post_run(request: Request) -> RunResponse:
    """body：{"spec": QuerySpec, "plan_id": "..."}，plan_id 可选。"""
    body, issues = await _read_body(request)
    if body is None:
        return _revise(*issues)
    # 计算要几秒，放到线程池里，别卡住同时进来的其他请求（比如查同步进度）
    return await run_in_threadpool(execute, body["spec"], body.get("plan_id"), services_of(request))


def check(raw_spec: object, plan_id: str | None, services: Services) -> CheckResponse:
    status = services.data_status()
    if not status.ready:
        return CheckResponse(
            status="data_not_ready", message=status.reason, data=_progress(status, services)
        )
    spec, issues = check_spec(raw_spec, services.ds, services.events)
    if spec is None:
        return CheckResponse(status="needs_revision", issues=issues)
    try:
        spec, confirm = _explained(spec, services, plan_id)
    except (MissingDataError, ExprDataError) as exc:
        return CheckResponse(status="needs_revision", issues=[Issue(message=str(exc))])
    return CheckResponse(
        status="ok",
        spec=spec.model_dump(mode="json", by_alias=True),
        summary=confirm.summary,
        assumptions=assumption_items(confirm),
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
    steps: list[dict[str, object]] = [
        {"step": "check_spec", "kind": spec.output.kind, "plan_id": plan_id, "issues": []}
    ]
    try:
        mark = time.perf_counter()
        spec, confirm = _explained(spec, services, plan_id)
        steps.append({"step": "explain", "assumptions": len(confirm.items), "ms": _ms(mark)})
        mark = time.perf_counter()
        result = run_research(spec, services.ds)
        steps.append({"step": "research.run", "ms": _ms(mark)})
    except (MissingDataError, ExprDataError) as exc:
        return _revise(Issue(message=str(exc)))
    except Exception as exc:  # noqa: BLE001 —— 算不出来也要给用户一个记录编号，不能只回 500
        error = f"{type(exc).__name__}: {exc}"
        run_id = services.store.save_run(
            _record(spec, plan_id, status.data_through, started, status="failed", error=error)
        )
        logger.exception("计算出错，运行记录 %s", run_id)
        steps.append({"step": "respond", "status": "failed", "error": error})
        _save_trace(services, run_id, plan_id, steps)
        message = f"计算时出错了，运行记录编号 {run_id}，详情见服务日志"
        return RunResponse(status="failed", run_id=run_id, message=message)

    payload = result_to_dict(result)
    # 确认卡上那份说明跟着结果一起存：结果页上方的「我把你的问题理解成」、卡底下的「怎么算的」都用它
    payload["understood"] = confirm.summary
    payload["assumptions"] = [item.model_dump() for item in assumption_items(confirm)]
    if spec.output.kind == "card":
        # 小结不在这里写：卡先出，页面再调 POST /api/run/{run_id}/narrative 取小结
        payload["narrate"] = bool(spec.narrate and services.llm is not None)
    run_id = services.store.save_run(
        _record(spec, plan_id, status.data_through, started, status="done", result=payload)
    )
    steps.append({"step": "respond", "status": "done", **_size(payload), "ms": _ms(started)})
    _save_trace(services, run_id, plan_id, steps)
    return RunResponse(status="done", run_id=run_id, result=payload)


def _question(services: Services, plan_id: str | None) -> str | None:
    """用户原话：卡下面那段话要先回答用户问的。表单直接提交的没有原话。"""
    record = services.store.get_plan(plan_id) if plan_id else None
    return str(record.detail.get("question") or record.query) if record else None


def call_step(call: LLMCall, step: str) -> dict[str, object]:
    """一次大模型调用 → 过程记录里的一步。提示词只记哈希，原始返回整份记（LLMCall 的说明）。"""
    return {
        "step": step,
        "attempt": call.attempt,
        "prompt_id": call.prompt_id,
        "prompt_version": call.prompt_version,
        "rendered_hash": call.rendered_hash,
        "model": call.model,
        "user_message": call.user_message,
        "raw_reply": call.raw_reply,
        "input_tokens": call.input_tokens,
        "cached_tokens": call.cached_tokens,
        "output_tokens": call.output_tokens,
        "seconds": call.seconds,
        "problems": list(call.problems),
        "error": call.error,
    }


def _ms(mark: float) -> int:
    return round((time.perf_counter() - mark) * 1000)


def _size(payload: dict[str, Any]) -> dict[str, object]:
    """结果多大：表看满足条件几个、取回几行，卡看几个标的，统计看触发了几次。"""
    if "total" in payload:
        return {"total": payload["total"], "rows": len(payload.get("rows") or ())}
    if payload["kind"] == "card":
        return {"cards": len(payload.get("items") or ())}
    return {"triggers": len(payload.get("triggers") or ())}


def _save_trace(
    services: Services, run_id: str, plan_id: str | None, steps: list[dict[str, object]]
) -> None:
    """记录失败不能影响回答：结果已经算好存好了，过程记录只是给开发看的。

    query 记用户原话，和提问那条过程记录一样；不经过提问直接调接口的没有原话，是空串。
    """
    try:
        question = _question(services, plan_id) or ""
        services.store.save_trace(TraceRecord(record_id=run_id, query=question, steps=steps))
    except Exception:  # noqa: BLE001 —— 存不下就算了，只记一条日志
        logger.warning("运行 %s 的过程记录没能存下来", run_id, exc_info=True)


def _append_trace(services: Services, run_id: str, steps: list[dict[str, object]]) -> None:
    """小结写完后，把写它的每次大模型调用接在这次运行的过程记录后面。失败只记日志。"""
    try:
        trace = services.store.get_trace(run_id) or TraceRecord(record_id=run_id, query="")
        services.store.save_trace(replace(trace, steps=[*trace.steps, *steps]))
    except Exception:  # noqa: BLE001
        logger.warning("运行 %s 的小结没能记进过程记录", run_id, exc_info=True)


@router.get("/api/run/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, object]:
    """运行记录：spec、结果或错误、数据截至哪天、事件库版本、耗时。卡写过小结的，带上小结。"""
    store = services_of(request).store
    record = store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"没有编号为 {run_id} 的运行记录")
    narrative = store.get_narrative(run_id)
    return {**asdict(record), "narrative": None if narrative is None else narrative.text}


@router.post("/api/run/{run_id}/narrative")
async def post_narrative(run_id: str, request: Request) -> NarrativeResponse:
    """卡下面的小结（DESIGN.md §1.6）。卡先出，页面再调这里；大模型写一次十几秒到一分钟。

    写过就直接给存下的那段，不重写——小结跟着这次运行，分享链接、翻记录看到的是同一段。
    """
    return await run_in_threadpool(write_narrative, run_id, services_of(request))


#: 同一次运行的小结一次只写一个：页面同时发来两次（开发时 React 的严格模式会把取小结做两遍，
#: 两个人同时打开同一个分享链接也会），后到的等先到的写完，拿存下的那段，不再调一次大模型
_narrating: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


def write_narrative(run_id: str, services: Services) -> NarrativeResponse:
    with _narrating[run_id]:
        return _write_narrative(run_id, services)


def _write_narrative(run_id: str, services: Services) -> NarrativeResponse:
    record = services.store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"没有编号为 {run_id} 的运行记录")
    result = record.result or {}
    if result.get("kind") != "card" or not record.spec.get("narrate"):
        raise HTTPException(status_code=400, detail="这次运行不是要写小结的卡")
    if saved := services.store.get_narrative(run_id):
        return NarrativeResponse(text=saved.text, error=saved.error)
    if services.llm is None:
        return NarrativeResponse(error="大模型没有配置好")
    narration = narrate(result, _question(services, record.plan_id), services.llm)
    _append_trace(services, run_id, [call_step(call, "llm.narrate") for call in narration.calls])
    try:
        services.store.save_narrative(
            NarrativeRecord(run_id=run_id, text=narration.text, error=narration.error)
        )
    except Exception:  # noqa: BLE001 —— 存不下来也把这次写好的给用户，下次再要会重写
        logger.warning("运行 %s 的小结没能存下来", run_id, exc_info=True)
    return NarrativeResponse(text=narration.text, error=narration.error)


@router.get("/api/traces/{record_id}")
def get_trace(record_id: str, request: Request) -> dict[str, object]:
    """过程记录：这次提问 / 运行的每一步调了什么、返回了什么、花了多久。

    编号用 plan_id 或 run_id。给开发排查用——「大模型到底回了什么」以前只能靠猜。
    """
    record = services_of(request).store.get_trace(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"没有编号为 {record_id} 的过程记录")
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


def assumption_items(confirm: Confirm) -> list[AssumptionItem]:
    return [
        AssumptionItem(group=item.group, field=item.field, text=item.text, default=item.default)
        for item in confirm.items
    ]


def _explained(spec: Spec, services: Services, plan_id: str | None) -> tuple[Spec, Confirm]:
    """说明文字写进 spec.assumptions，运行记录里存的就是确认卡上那份。
    带 plan_id 时，没改过的栏目继续用提问原话的说法。"""
    record = services.store.get_plan(plan_id) if plan_id else None
    mentions = plan_mentions(spec.model_dump(mode="json", by_alias=True), record)
    confirm = explain(spec, services.ds, mentions)
    texts = tuple(item.text for item in confirm.items)
    return spec.model_copy(update={"assumptions": texts}), confirm


def _progress(status: DataStatus, services: Services) -> dict[str, object]:
    return {"status": to_jsonable(status), "sync": services.sync_job.snapshot()}


def _record(
    spec: Spec, plan_id: str | None, data_through: str | None, started: float, **outcome: object
) -> RunRecord:
    return RunRecord(
        spec=spec.model_dump(mode="json", by_alias=True),
        plan_id=plan_id,
        data_through=data_through,
        library_version=(
            spec.output.event.library_version if isinstance(spec.output, EventStudyOutput) else None
        ),
        duration_ms=round((time.perf_counter() - started) * 1000),
        **outcome,  # type: ignore[arg-type]
    )


def _revise(*issues: Issue) -> RunResponse:
    return RunResponse(status="needs_revision", issues=list(issues))
