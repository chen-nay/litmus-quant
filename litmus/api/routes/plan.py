"""POST /api/plan：中文提问 → 确认卡 / 澄清卡 / 改写建议（ARCHITECTURE §1.4 ①、§5.2、§6）。

1. 本地数据不够 → data_not_ready，不调大模型
2. 大模型没配好 → failed，说明缺什么
3. llm.plan()：大模型填查询条件草稿；表达式、事件、结构不对时带着问题重试一次（防线②）
4. 防线③：股票、概念板块按原话查代码，原话查不到再用大模型猜的名字查（可以猜几个，用「、」隔开）。
   只有一个、或者只有一个代码 / 名称完全一致的，直接用；对应多个转成澄清让用户选；概念板块都查不到时
   列出名字相近的让用户选。再过和 /api/check 同一套检查，不过也转成澄清
5. 生成说明文字（带上原话里的说法），存下这次提问 → plan_id。确认卡上检查、运行时带上 plan_id，
   没改过的栏目继续用原话的说法（explain.plan_mentions）
6. 追问：把 previous_plan_id 带回来，这次说的话当成对追问的回答
"""

from __future__ import annotations

import json
import re
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from litmus.api.checks import check_spec, issue_text
from litmus.api.explain import explain
from litmus.api.models import AssumptionItem, Candidate, PlanQuestion, PlanResponse
from litmus.api.serialize import to_jsonable
from litmus.api.services import Services, services_of
from litmus.data import (
    CONCEPT,
    STOCK,
    SW_INDUSTRY,
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
from litmus.store import PlanRecord

router = APIRouter()

#: 问题最长多少个字
MAX_QUERY = 500

#: 候选最多列几个
MAX_CANDIDATES = 20


@router.post("/api/plan")
async def post_plan(request: Request) -> PlanResponse:
    """body：{"query": "昨天哪个股票成交量明显放大？", "previous_plan_id": "..."}，回答追问时带 previous_plan_id。

    大模型带思考，一次十几到几十秒（§5.1）。请求体写错返回 400。"""
    try:
        body = json.loads(await request.body())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='请求体要是 JSON，如 {"query": "..."}') from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail='请求体要是一个对象，如 {"query": "..."}')
    unknown = sorted(set(body) - {"query", "previous_plan_id"})
    if unknown:
        raise HTTPException(status_code=400, detail=f"不认识的栏目：{'、'.join(unknown)}")
    query, previous = body.get("query"), body.get("previous_plan_id")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="query 要填问题")
    if len(query) > MAX_QUERY:
        raise HTTPException(status_code=400, detail=f"问题太长了，最多 {MAX_QUERY} 个字")
    if previous is not None and not isinstance(previous, str):
        raise HTTPException(status_code=400, detail="previous_plan_id 要是文字")
    return await run_in_threadpool(make_plan, query.strip(), previous, services_of(request))


def make_plan(query: str, previous_plan_id: str | None, services: Services) -> PlanResponse:
    status = services.data_status()
    if not status.ready:
        return PlanResponse(
            status="data_not_ready", message=status.reason, data=_progress(status, services)
        )
    if services.llm is None:
        return PlanResponse(status="failed", message=_llm_missing())

    previous = None
    if previous_plan_id:
        record = services.store.get_plan(previous_plan_id)
        if record is None:
            return PlanResponse(
                status="failed", message=f"没有编号为 {previous_plan_id} 的提问记录"
            )
        previous = PreviousTurn(
            str(record.detail.get("question") or record.query),
            tuple(
                Question(str(item["question"]), tuple(item["options"]))
                for item in record.detail.get("questions", [])
            ),
        )

    ds = services.ds
    first, last = ds.data_range(STOCK)
    today = date.today()
    context = PlanContext(
        today=today,
        latest_trading_day=last,
        history_from=first,
        targets=ds.available_targets(),
        industries=tuple(board.name for board in ds.list_boards(SW_INDUSTRY)),
        # 日期换算表要数到去年最后一个交易日（今年以来）
        trading_days=tuple(ds.get_trading_calendar(date(today.year - 1, 12, 1), last)),
    )
    result = plan(query, context, services.llm, services.events, previous)
    mentions = _with_names(result)
    response = _respond(result, mentions, services)

    detail = {
        # 多轮追问时把回答接在原问题后面，下一轮追问用它
        "question": query if previous is None else f"{previous.query}；用户补充：{query}",
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
    return response.model_copy(update={"plan_id": plan_id})


def _with_names(result: PlanResult) -> tuple[Mention, ...]:
    """股票、概念板块的原话大模型已经单独给了（stock_mention、board_mention），它没在 mentions 里再记一遍的由代码补上。

    2026-09-15 实测：同一句「平安每次放量之后一周涨跌怎样」，大模型有时一个说法都不给，确认卡就只能写「股票：中国平安」。
    """
    fields = {mention.field for mention in result.mentions}
    extra = []
    if result.stock is not None and "target" not in fields:
        extra.append(Mention(result.stock.mention, "target"))
    if result.board is not None and "universe.board" not in fields:
        extra.append(Mention(result.board.mention, "universe.board"))
    return (*result.mentions, *extra)


def _respond(result: PlanResult, mentions: tuple[Mention, ...], services: Services) -> PlanResponse:
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

    ds = services.ds
    spec = dict(result.spec or {})
    if result.stock is not None:
        stocks = _decisive(_lookup(ds.resolve_stock, result.stock))
        if len(stocks) != 1:
            return _choose_stock(spec, result.stock, stocks)
        spec["target"] = {
            "mention": result.stock.mention,
            "guess": result.stock.guess,
            "code": stocks[0].code,
        }
    if result.board is not None:
        boards = _decisive(_lookup(lambda text: ds.resolve_board(text, CONCEPT), result.board))
        if len(boards) != 1:
            return _choose_board(spec, result.board, boards, ds)
        spec["universe"] = {
            **(spec.get("universe") or {}),
            "board": {"type": "concept", "code": boards[0].code},
        }

    checked, issues = check_spec(spec, ds, services.events)
    if checked is None:
        message = "；".join(issue_text(issue) for issue in issues)
        return PlanResponse(status=CLARIFY, spec=spec, message=f"条件要改一下：{message}")
    try:
        assumptions = explain(checked, ds, mentions)
    except (MissingDataError, ExprDataError) as exc:
        return PlanResponse(status=CLARIFY, spec=spec, message=f"条件要改一下：{exc}")
    checked = checked.model_copy(update={"assumptions": tuple(item.text for item in assumptions)})
    return PlanResponse(
        status=OK,
        spec=checked.model_dump(mode="json", by_alias=True),
        assumptions=[
            AssumptionItem(field=item.field, text=item.text, default=item.default)
            for item in assumptions
        ],
    )


def _lookup(resolve, name: NameMention) -> list:
    """先按原话查，查不到再按大模型猜的名字查（外号：通达信没有「光模块」，叫「光通信」「CPO概念」）。
    猜测名可以有几个，用「、」隔开，查到的合在一起。"""
    matches = resolve(name.mention)
    if matches or not name.guess:
        return matches
    found: dict[str, object] = {}
    for guess in re.split(r"[、,，]", name.guess):
        for match in resolve(guess.strip()) if guess.strip() else []:
            found.setdefault(match.code, match)
    return list(found.values())


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
        message = f"「{name.mention}」对应 {len(boards)} 个概念板块，选一个"
        candidates = _board_candidates(boards, "通达信概念板块")
        return PlanResponse(status=CLARIFY, spec=spec, message=message, board_candidates=candidates)
    similar = ds.similar_boards(name.mention, CONCEPT)
    if not similar:
        message = f"没找到「{name.mention}」这个概念板块：换个说法，或者打开表单在概念板块里选"
        return PlanResponse(status=CLARIFY, spec=spec, message=message)
    message = f"没找到叫「{name.mention}」的概念板块，下面几个名字相近，选一个；都不是就换个说法"
    candidates = _board_candidates(similar, "名字相近")
    return PlanResponse(status=CLARIFY, spec=spec, message=message, board_candidates=candidates)


def _board_candidates(boards: list[BoardMatch], note: str) -> list[Candidate]:
    return [
        Candidate(code=board.code, name=board.name, note=note) for board in boards[:MAX_CANDIDATES]
    ]


def _llm_missing() -> str:
    try:
        LLMConfig.from_env()
    except LLMError as exc:
        return f"大模型没有配置好：{exc}"
    return "大模型没有配置好：服务启动时没能创建大模型客户端，改好 .env 后重启服务"


def _progress(status: DataStatus, services: Services) -> dict[str, object]:
    return {"status": to_jsonable(status), "sync": services.sync_job.snapshot()}
