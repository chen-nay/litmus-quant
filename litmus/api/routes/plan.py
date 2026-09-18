"""POST /api/plan：中文提问 → 确认卡 / 澄清卡 / 改写建议（ARCHITECTURE §1.4 ①、§5.2、§6）。

1. 本地数据不够 → data_not_ready，不调大模型
2. 大模型没配好 → failed，说明缺什么
3. llm.plan()：大模型填查询条件草稿；表达式、事件、结构不对时带着问题重试一次（防线②）
4. 防线③：股票按原话查代码，查不到再用大模型猜的名字查；只有一个、或者只有一个代码 / 名称完全一致的直接用，
   对应多个转成澄清让用户选。板块在申万一级、二级行业和通达信概念里一起查，规则见 _pick_board（2026-09-15 定）；
   都查不到时列出名字相近的让用户选。再过和 /api/check 同一套检查，不过也转成澄清
5. 生成说明文字（带上原话里的说法），存下这次提问 → plan_id。确认卡上检查、运行时带上 plan_id，
   没改过的栏目继续用原话的说法（explain.plan_mentions）
6. 追问：把 previous_plan_id 带回来，这次说的话当成对追问的回答
7. 确认卡上用一句话改条件：把 spec（现在的条件）一起带回来，这次说的话当成「要改哪里」。
   大模型在这份条件上改，不从原话重新生成——之前选过的候选、表单上改过的不会丢（2026-09-16 加）
8. 每一步都记进过程记录（store 的 traces/<plan_id>.json）：调了几次大模型、发了什么、原始返回是什么、
   token 和耗时、防线②发现的问题、防线③怎么解析的名字、检查有没有过。**记录失败不影响回答**
   （2026-09-16 加）。为什么要记：确认卡以下是纯代码所以可复现，确认卡以上（问题 → 查询单）
   既不可复现也看不见——同一个问题明天再问，日期表和行业清单都变了，没有记录就还原不了当时那一次
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from litmus.api.checks import check_spec, issue_text
from litmus.api.explain import explain
from litmus.api.models import Candidate, PlanQuestion, PlanResponse
from litmus.api.routes.runs import assumption_items, call_step, execute
from litmus.api.serialize import to_jsonable
from litmus.api.services import Services, services_of
from litmus.data import (
    CONCEPT,
    STOCK,
    SW_INDUSTRY,
    SW_INDUSTRY_L2,
    BoardMatch,
    DataService,
    DataStatus,
    MissingDataError,
    StockMatch,
)
from litmus.expr import ExprDataError
from litmus.llm import (
    CLARIFY,
    FAILED,
    OK,
    LLMConfig,
    LLMError,
    NameMention,
    PlanContext,
    PlanResult,
    PreviousTurn,
    Question,
    plan,
)
from litmus.spec import Mention
from litmus.store import PlanRecord, TraceRecord

logger = logging.getLogger(__name__)

router = APIRouter()

#: 问题最长多少个字
MAX_QUERY = 500

#: 候选最多列几个
MAX_CANDIDATES = 20


@router.post("/api/plan")
async def post_plan(request: Request) -> PlanResponse:
    """body：{"query": "...", "previous_plan_id": "...", "spec": {...}}。

    回答追问时带 previous_plan_id；确认卡上改条件时再带一个 spec（现在的条件），这次说的话当成要改哪里。
    大模型带思考，一次十几到几十秒（§5.1）。请求体写错返回 400。"""
    try:
        body = json.loads(await request.body())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='请求体要是 JSON，如 {"query": "..."}') from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail='请求体要是一个对象，如 {"query": "..."}')
    unknown = sorted(set(body) - {"query", "previous_plan_id", "spec"})
    if unknown:
        raise HTTPException(status_code=400, detail=f"不认识的栏目：{'、'.join(unknown)}")
    query, previous, spec = body.get("query"), body.get("previous_plan_id"), body.get("spec")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="query 要填问题")
    if len(query) > MAX_QUERY:
        raise HTTPException(status_code=400, detail=f"问题太长了，最多 {MAX_QUERY} 个字")
    if previous is not None and not isinstance(previous, str):
        raise HTTPException(status_code=400, detail="previous_plan_id 要是文字")
    if spec is not None and not isinstance(spec, dict):
        raise HTTPException(status_code=400, detail="spec 要是一个对象，就是确认卡上现在的条件")
    return await run_in_threadpool(make_plan, query.strip(), previous, services_of(request), spec)


def make_plan(
    query: str,
    previous_plan_id: str | None,
    services: Services,
    spec: dict[str, object] | None = None,
) -> PlanResponse:
    status = services.data_status()
    if not status.ready:
        return PlanResponse(
            status="data_not_ready", message=status.reason, data=_progress(status, services)
        )
    if services.llm is None:
        return PlanResponse(status="failed", message=_llm_missing())

    previous, before = None, None
    if previous_plan_id:
        record = services.store.get_plan(previous_plan_id)
        if record is None:
            return PlanResponse(
                status="failed", message=f"没有编号为 {previous_plan_id} 的提问记录"
            )
        before = str(record.detail.get("question") or record.query)
        # 带了 spec 就是在现有条件上改，不走追问：那条路会让大模型从原话重新生成
        if spec is None:
            previous = PreviousTurn(
                before,
                tuple(
                    Question(str(item["question"]), tuple(item["options"]))
                    for item in record.detail.get("questions", [])
                ),
            )

    ds = services.ds
    first, last = ds.data_range(STOCK)
    today = date.today()
    targets = ds.available_targets()
    context = PlanContext(
        today=today,
        latest_trading_day=last,
        history_from=first,
        targets=targets,
        industries=tuple(board.name for board in ds.list_boards(SW_INDUSTRY)),
        # 日期换算表要数到去年最后一个交易日（今年以来）
        trading_days=tuple(ds.get_trading_calendar(date(today.year - 1, 12, 1), last)),
        industries_l2=tuple(
            (board.parent or "", board.name) for board in ds.list_boards(SW_INDUSTRY_L2)
        )
        if SW_INDUSTRY_L2 in targets
        else (),
    )
    revise = _for_revise(spec) if spec is not None else None
    result = plan(query, context, services.llm, services.events, previous, revise)
    mentions = _with_names(result)
    steps: list[dict[str, object]] = [call_step(call, "llm.plan") for call in result.calls]
    response = _respond(result, mentions, services, steps)

    detail = {
        # 多轮追问、多次修改时把这次说的话接在后面，下一轮追问用它
        "question": _chained(before, query, spec is not None),
        "previous_plan_id": previous_plan_id,
        "questions": to_jsonable(list(result.questions)),
        "mentions": [{"phrase": m.phrase, "field": m.field} for m in mentions],
        "message": response.message,
        "alternatives": response.alternatives,
        "stock_candidates": [c.model_dump() for c in response.stock_candidates],
        "board_candidates": [c.model_dump() for c in response.board_candidates],
        "attempts": result.attempts,
        "prompt_version": result.prompt_version,
        "error": result.error,
    }
    plan_id = services.store.save_plan(
        PlanRecord(query=query, status=response.status, spec=response.spec, detail=detail)
    )
    if response.status == OK and (response.spec or {}).get("output", {}).get("kind") == "card":
        response = _card(response, plan_id, services, steps)
    steps.append({"step": "respond", "status": response.status, "message": response.message})
    _save_trace(services, plan_id, query, steps)
    return response.model_copy(update={"plan_id": plan_id})


def _card(
    response: PlanResponse, plan_id: str, services: Services, steps: list[dict[str, object]]
) -> PlanResponse:
    """卡不走确认卡，提问这一步就算完，结果跟着回去（DESIGN.md §1.5）。"""
    run = execute(response.spec, plan_id, services)
    steps.append({"step": "run", "run_id": run.run_id, "status": run.status})
    if run.status == "done":
        # 一句话总结和说明跟着结果走（卡底下的「怎么算的」），不再放一份在外面
        return response.model_copy(
            update={
                "status": "done",
                "run_id": run.run_id,
                "result": run.result,
                "summary": "",
                "assumptions": [],
            }
        )
    if run.status == "needs_revision":
        message = "；".join(issue_text(issue) for issue in run.issues)
        return PlanResponse(status=CLARIFY, spec=response.spec, message=f"条件要改一下：{message}")
    return PlanResponse(
        status=run.status, spec=response.spec, message=run.message, run_id=run.run_id, data=run.data
    )


def _save_trace(
    services: Services, plan_id: str, query: str, steps: list[dict[str, object]]
) -> None:
    """记录失败不能影响回答：过程记录是给开发看的，用户的答案已经算好了。"""
    try:
        services.store.save_trace(TraceRecord(record_id=plan_id, query=query, steps=steps))
    except Exception:  # noqa: BLE001 —— 存不下就算了，只记一条日志
        logger.warning("提问 %s 的过程记录没能存下来", plan_id, exc_info=True)


#: 确认卡上的条件里，这两栏是后端算出来的结果，不给大模型看
_SPEC_META = ("assumptions", "defaults_used")


def _for_revise(spec: dict[str, object]) -> dict[str, object]:
    """确认卡上现在的条件，交给大模型改。

    默认值由前端去掉（specForm.dropUntouchedDefaults），这样没改到的栏目重新走一遍补默认值，
    确认卡上照样标「默认值，可修改」；没去掉也只是少标几栏，不影响算出来的结果。

    股票只留代码，原话和猜测名去掉：2026-09-16 实测大模型看见 target 里的 mention 就照抄，
    于是又按名字重查一遍——换成「平安」这种对应多只的，改一次条件就要重选一次股票。
    """
    trimmed = {key: value for key, value in spec.items() if key not in _SPEC_META}
    subject = trimmed.get("subject")
    if isinstance(subject, dict) and subject.get("codes"):
        trimmed["subject"] = {"kind": "codes", "codes": subject["codes"]}
    return trimmed


def _chained(before: str | None, query: str, revised: bool) -> str:
    """存进记录的问题：多轮下来接成一句，下一轮追问要用它当「原来的问题」。"""
    if before is None:
        return query
    return f"{before}；用户{'又改' if revised else '补充'}：{query}"


def _with_names(result: PlanResult) -> tuple[Mention, ...]:
    """点名的标的、限定的板块，原话大模型已经单独给了（subject.mentions、scope.board），
    它没在 mentions 里再记一遍的由代码补上。

    2026-09-15 实测：同一句「平安每次放量之后一周涨跌怎样」，大模型有时一个说法都不给，确认卡就只能写「股票：中国平安」。
    """
    fields = {mention.field for mention in result.mentions}
    extra = []
    if "subject" not in fields:
        extra += [Mention(name.mention, "subject") for name in result.subjects if not name.is_code]
    if result.board is not None and not result.board.is_code and "scope.board" not in fields:
        extra.append(Mention(result.board.mention, "scope.board"))
    return (*result.mentions, *extra)


def _respond(
    result: PlanResult,
    mentions: tuple[Mention, ...],
    services: Services,
    steps: list[dict[str, object]] | None = None,
) -> PlanResponse:
    if result.status == FAILED:
        return PlanResponse(
            status="failed", message=f"没能把这个问题翻译成查询条件：{result.error}"
        )
    if result.status == CLARIFY:
        questions = [
            PlanQuestion(question=q.question, options=list(q.options)) for q in result.questions
        ]
        return PlanResponse(status=CLARIFY, questions=questions)
    if result.status != OK:
        return PlanResponse(
            status=result.status, message=result.message, alternatives=list(result.alternatives)
        )

    note = steps.append if steps is not None else (lambda _step: None)
    ds = services.ds
    spec = dict(result.spec or {})
    if result.subjects:
        if clarify := _resolve_subjects(spec, result.subjects, ds, note):
            return clarify
    if result.board is not None:
        picked, candidates = _pick_board(ds, result.board)
        note(
            {
                "step": "resolve_board",
                "mention": result.board.mention,
                "guess": result.board.guess,
                "by_code": result.board.is_code,
                "picked": None if picked is None else {"code": picked.code, "name": picked.name},
                "candidates": [
                    {"code": m.code, "name": m.name, "type": m.board_type}
                    for m in candidates[:MAX_CANDIDATES]
                ],
            }
        )
        if picked is None:
            return _choose_board(spec, result.board, candidates, ds)
        spec["scope"] = _with_board(spec.get("scope") or {}, picked)

    checked, issues = check_spec(spec, ds, services.events)
    note({"step": "check_spec", "issues": [issue_text(issue) for issue in issues]})
    if checked is None:
        message = "；".join(issue_text(issue) for issue in issues)
        return PlanResponse(status=CLARIFY, spec=spec, message=f"条件要改一下：{message}")
    if checked.output.kind == "card":
        # 卡不走确认卡，由 _card 直接算。交出去的是补默认值之前的条件：那边再检查一遍时
        # 才认得出哪些栏目用了默认值，卡底下的「怎么算的」照样标「默认」
        return PlanResponse(status=OK, spec=spec)
    try:
        confirm = explain(checked, ds, mentions)
    except (MissingDataError, ExprDataError) as exc:
        return PlanResponse(status=CLARIFY, spec=spec, message=f"条件要改一下：{exc}")
    checked = checked.model_copy(update={"assumptions": tuple(i.text for i in confirm.items)})
    return PlanResponse(
        status=OK,
        spec=checked.model_dump(mode="json", by_alias=True),
        summary=confirm.summary,
        assumptions=assumption_items(confirm),
    )


def _resolve_subjects(
    spec: dict, names: tuple[NameMention, ...], ds: DataService, note
) -> PlanResponse | None:
    """点名的标的按算的范围是股票还是板块去查，查准的填进 subject.codes。

    有一个对应多个、或者查不到，就停下来让用户选（一次问一个）；查准了的已经填好，选完不用再查。
    返回 None 表示都查准了。
    """
    target = (spec.get("scope") or {}).get("target") or STOCK
    if target == STOCK:
        resolve = ds.resolve_stock
    else:

        def resolve(text: str) -> list:
            return ds.resolve_board(text, target)

    codes, said, pending = [], [], None
    for name in names:
        matches = _lookup(resolve, name)
        note(
            {
                "step": "resolve_stock" if target == STOCK else "resolve_board",
                "mention": name.mention,
                "guess": name.guess,
                "by_code": name.is_code,
                "matches": [{"code": m.code, "name": m.name} for m in matches[:MAX_CANDIDATES]],
            }
        )
        picked = _picked(matches, target)
        if picked is not None:
            if picked.code in codes:
                continue  # 「茅台」「贵州茅台」说的是同一只
            codes.append(picked.code)
            # 照抄代码进来的（改现有条件）没有原话，和表单改过条件后一样只带代码
            if not name.is_code:
                said.append({"mention": name.mention, "guess": name.guess})
        elif pending is None:
            pending = (name, matches)
    spec["subject"] = {"kind": "codes", "mentions": said, **({"codes": codes} if codes else {})}
    if pending is None:
        return None
    name, matches = pending
    if target == STOCK:
        return _choose_stock(spec, name, matches)
    return _choose_subject_board(spec, name, matches, target)


def _choose_subject_board(
    spec: dict, name: NameMention, boards: list[BoardMatch], target: str
) -> PlanResponse:
    label = _BOARD_LABELS[target]
    if not boards:
        message = f"没找到叫「{name.mention}」的{label}：换个说法，或者打开表单选"
        return PlanResponse(status=CLARIFY, spec=spec, message=message)
    if not any(board.exact for board in boards):
        message = (
            f"没有叫「{name.mention}」的{label}，名字相近的是下面这些，选一个；都不是就换个说法"
        )
    else:
        message = f"「{name.mention}」对应 {len(boards)} 个{label}，选一个"
    return PlanResponse(
        status=CLARIFY, spec=spec, message=message, board_candidates=_board_candidates(boards)
    )


def _lookup(resolve, name: NameMention) -> list:
    """先按原话查，查不到再按大模型猜的名字查（外号：通达信没有「光模块」，叫「光通信」「CPO概念」）。
    猜测名可以有几个，用「、」隔开，查到的合在一起。"""
    matches = resolve(name.mention)
    if matches:
        return matches
    return _unique([match for guess in _guesses(name.guess) for match in resolve(guess)])


def _guesses(text: str | None) -> list[str]:
    return [part.strip() for part in re.split(r"[、,，]", text or "") if part.strip()]


def _unique(matches: list) -> list:
    """去重，保持先后：同一个口径下的同一个代码只留第一次出现的。"""
    seen, kept = set(), []
    for match in matches:
        key = (getattr(match, "board_type", ""), match.code)
        if key not in seen:
            seen.add(key)
            kept.append(match)
    return kept


def _pick_board(ds: DataService, name: NameMention) -> tuple[BoardMatch | None, list[BoardMatch]]:
    """板块口径的规则（2026-09-15 定）：申万一级、二级行业和通达信概念板块一起查。返回 (直接用的, 让用户选的)。

    - 原话完全对上的只有一个，就用它：「半导体板块」→ 申万二级「半导体」，「银行股」→ 申万一级「银行」
    - 原话一个都没对上，大模型猜的名字完全对上的只有一个，也用它：「光模块」→ 猜「光通信」
    - 其他都交给用户选，候选写明口径：两边都完全对上的，只是名称包含对上的（「半导体」只包含在「第三代半导体」里，
      2026-09-15 实测直接用了它，结果范围窄得离谱），猜了几个都对上的
    """
    said = ds.resolve_board(name.mention)
    exact = [match for match in said if match.exact]
    if len(exact) == 1:
        return exact[0], []
    guessed = [match for guess in _guesses(name.guess) for match in ds.resolve_board(guess)]
    guessed_exact = _unique([match for match in guessed if match.exact])
    if not said and len(guessed_exact) == 1:
        return guessed_exact[0], []
    return None, _unique(said + guessed)


def _with_board(scope: dict, board: BoardMatch) -> dict:
    """选中申万行业填算的范围里的行业，概念板块填板块。"""
    if board.board_type == CONCEPT:
        return {**scope, "board": {"type": "concept", "code": board.code}}
    return {**scope, "industry": board.name}


def _picked(matches: list, target: str):
    """能直接用的那一个，没有就是 None（交给用户选）。

    股票：只有一个候选，或者只有一个代码 / 名称完全一致的。板块：只认完全一致的——
    「新能源」只包含在「新能源车」里就直接用了，2026-09-18 实测看的其实是另一个板块。
    """
    chosen = _decisive(matches) if target == STOCK else [m for m in matches if m.exact]
    return chosen[0] if len(chosen) == 1 else None


def _decisive(matches: list) -> list:
    """候选里只有一个代码或名称完全一致的，就是它；否则全部交给用户选。"""
    exact = [match for match in matches if match.exact]
    return exact if len(exact) == 1 else matches


def _choose_stock(spec: dict, name: NameMention, matches: list[StockMatch]) -> PlanResponse:
    if not matches:
        message = f"没找到「{name.mention}」这只股票：换个说法，或者直接写代码（如 600519.SH）"
        return PlanResponse(status=CLARIFY, spec=spec, message=message)
    candidates = [
        Candidate(
            code=match.code,
            name=match.name,
            note=match.rule_label + ("，已退市" if match.delisted else ""),
        )
        for match in matches[:MAX_CANDIDATES]
    ]
    message = f"「{name.mention}」对应 {len(matches)} 只股票，选一只"
    return PlanResponse(status=CLARIFY, spec=spec, message=message, stock_candidates=candidates)


def _choose_board(
    spec: dict, name: NameMention, boards: list[BoardMatch], ds: DataService
) -> PlanResponse:
    if boards:
        message = f"「{name.mention}」可能是下面这些行业或板块，选一个"
        candidates = _board_candidates(boards)
        return PlanResponse(status=CLARIFY, spec=spec, message=message, board_candidates=candidates)
    available = ds.available_targets()
    similar = [
        match
        for kind in (SW_INDUSTRY, SW_INDUSTRY_L2, CONCEPT)
        if kind in available
        for match in ds.similar_boards(name.mention, kind)
    ]
    if not similar:
        message = (
            f"没找到「{name.mention}」这个行业或板块：换个说法，或者打开表单在行业、概念板块里选"
        )
        return PlanResponse(status=CLARIFY, spec=spec, message=message)
    message = f"没找到叫「{name.mention}」的行业或板块，下面几个名字相近，选一个；都不是就换个说法"
    candidates = _board_candidates(similar, "名字相近")
    return PlanResponse(status=CLARIFY, spec=spec, message=message, board_candidates=candidates)


#: 候选上写明口径
_BOARD_LABELS = {
    SW_INDUSTRY: "申万一级行业",
    SW_INDUSTRY_L2: "申万二级行业",
    CONCEPT: "通达信概念板块",
}


def _board_candidates(boards: list[BoardMatch], extra: str = "") -> list[Candidate]:
    return [
        Candidate(
            code=board.code,
            name=board.name,
            note=_BOARD_LABELS[board.board_type] + (f"，{extra}" if extra else ""),
            board_type=board.board_type,
        )
        for board in boards[:MAX_CANDIDATES]
    ]


def _llm_missing() -> str:
    try:
        LLMConfig.from_env()
    except LLMError as exc:
        return f"大模型没有配置好：{exc}"
    return "大模型没有配置好：服务启动时没能创建大模型客户端，改好 .env 后重启服务"


def _progress(status: DataStatus, services: Services) -> dict[str, object]:
    return {"status": to_jsonable(status), "sync": services.sync_job.snapshot()}
