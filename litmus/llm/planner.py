"""llm.plan()：中文提问 → 查询条件草稿（ARCHITECTURE §5.2）。

- 大模型填一份扁平的格式（OUTPUT_SCHEMA），代码转成 QuerySpec 的结构。2026-09-15 实测扁平格式能被火山引擎接受；
  QuerySpec 本身的格式（6000 字符、10 个子定义、oneOf）没有实测过
- 大模型只填用户说到的栏目，没说的由代码补默认值并在确认卡上标出来（§5.3、§5.4）
- 股票、概念板块只填原话和猜测名，不填代码，由 api 用 ds.resolve_stock / resolve_board 核对（§2.3）
- 防线②：表达式过 expr.parse + validate，事件编号和参数过 signals.render_event，结构过 spec.parse_spec。
  不过就带着问题清单用 planner.repair 重试一次，还不过返回 failed
- 实测大模型偶尔漏填 status：有问题清单就当澄清，填了形状就当 ok
- 改写建议只留几句像样的：条数、长度、禁用词、带百分比的丢掉（§5.2）
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError

from litmus.expr import ExprSyntaxError, field_catalog, operator_catalog, parse, validate
from litmus.llm.client import LLMClient, LLMError
from litmus.llm.models import (
    CLARIFY,
    FAILED,
    NOT_AN_EVENT,
    OK,
    UNSUPPORTED,
    NameMention,
    PlanContext,
    PlanResult,
    PreviousTurn,
    Question,
)
from litmus.llm.prompts import load_prompt
from litmus.signals import EventLibrary, EventParamError, event_catalog, render_event
from litmus.spec import DEFAULTS, Mention, parse_spec

logger = logging.getLogger(__name__)

#: 原话里的说法能对应的栏目（spec.render_assumptions 按这些栏目写「理解为」）
MENTION_FIELDS = (
    "as_of",
    "filter",
    "sort",
    "limit",
    "universe.base",
    "universe.industry",
    "universe.board",
    "universe.exclude",
    "board_type",
    "target",
    "event",
    "time_range",
    "horizons",
    "benchmark",
    "cost_bps",
)

_STR: dict[str, Any] = {"type": "string"}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": [OK, CLARIFY, UNSUPPORTED, NOT_AN_EVENT]},
        "shape": {"type": "string", "enum": ["stock_list", "board_list", "stock_history"]},
        "as_of": {"type": "string", "description": "YYYY-MM-DD"},
        "filter_expr": _STR,
        "filter_label": _STR,
        "sort_by": _STR,
        "sort_order": {"type": "string", "enum": ["asc", "desc"]},
        "sort_label": _STR,
        "limit": {"type": "integer"},
        "board_type": {"type": "string", "enum": ["sw_industry", "concept"]},
        "universe_base": {"type": "string", "enum": ["all_a", "hs300", "zz500"]},
        "industry": _STR,
        "board_mention": _STR,
        "board_guess": _STR,
        "stock_mention": _STR,
        "stock_guess": _STR,
        "event_id": _STR,
        "event_params": {"type": "object", "additionalProperties": {"type": "number"}},
        "time_from": {"type": "string", "description": "YYYY-MM-DD"},
        "time_to": {"type": "string", "description": "YYYY-MM-DD"},
        "horizons": {"type": "array", "items": {"type": "integer"}},
        "benchmark": {
            "type": "string",
            "enum": ["universe_equal_weight", "index:000300.SH", "index:000905.SH"],
        },
        "cost_bps": {"type": "number"},
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": _STR,
                    "field": {"type": "string", "enum": list(MENTION_FIELDS)},
                },
                "required": ["phrase", "field"],
            },
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": _STR, "options": {"type": "array", "items": _STR}},
                "required": ["question", "options"],
            },
        },
        "message": _STR,
        "alternatives": {"type": "array", "items": _STR},
    },
    "required": ["status"],
}

#: 改写建议里出现就丢掉：系统不给买卖建议、不做预测
_BANNED = ("买入", "卖出", "能买", "该买", "买吗", "推荐", "必涨", "抄底", "目标价", "预测")
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s*[%％]")
MAX_ALTERNATIVE_LENGTH = 40


def plan(
    query: str,
    context: PlanContext,
    client: LLMClient,
    events: EventLibrary,
    previous: PreviousTurn | None = None,
) -> PlanResult:
    """query：用户这次说的话；有 previous 时它是对上一轮追问的回答。"""
    system_prompt = load_prompt("planner.system")
    system = system_prompt.render(**system_variables(context, events))
    user = query
    if previous is not None:
        user = load_prompt("planner.followup").render(
            query=previous.query, questions=_questions_text(previous.questions), answer=query
        )
    version = system_prompt.version
    message, draft, problems = user, {}, []
    for attempt in (1, 2):
        if attempt == 2:
            message = load_prompt("planner.repair").render(
                query=user,
                previous_output=json.dumps(draft, ensure_ascii=False, indent=1),
                problems="\n".join(f"- {problem}" for problem in problems),
            )
        try:
            reply = client.structured(system, message, OUTPUT_SCHEMA)
        except LLMError as exc:
            logger.warning("planner.system@%s 第 %d 次调用失败：%s", version, attempt, exc)
            return PlanResult(FAILED, attempts=attempt, prompt_version=version, error=str(exc))
        logger.info(
            "planner.system@%s 第 %d 次：%.1f 秒，token %s / %s",
            version,
            attempt,
            reply.seconds,
            reply.input_tokens,
            reply.output_tokens,
        )
        draft = reply.data
        result, problems = read_output(draft, context, events)
        if not problems:
            return replace(result, attempts=attempt, prompt_version=version)
        logger.info("planner.system@%s 第 %d 次输出没通过检查：%s", version, attempt, problems)
    return PlanResult(FAILED, attempts=2, prompt_version=version, error="；".join(problems))


# ── 大模型的输出 → 结果 ─────────────────────────────────────────


def read_output(
    draft: Mapping[str, Any], context: PlanContext, events: EventLibrary
) -> tuple[PlanResult, list[str]]:
    """返回 (结果, 问题清单)。问题清单非空时结果不能用，要带着问题重试。"""
    questions = _questions(draft.get("questions"))
    status = _text(draft.get("status"))
    if not status:
        status = CLARIFY if questions else (OK if _text(draft.get("shape")) else "")
    if status not in (OK, CLARIFY, UNSUPPORTED, NOT_AN_EVENT):
        allowed = "ok、needs_clarification、unsupported、not_an_event"
        return PlanResult(FAILED), [f"status 只能是 {allowed}，收到 {draft.get('status')!r}"]
    mentions = _mentions(draft.get("mentions"))
    if status == CLARIFY:
        if not questions:
            return PlanResult(FAILED), [
                "status=needs_clarification 时 questions 至少要有一个问题，每个问题带 2~3 个选项"
            ]
        return PlanResult(CLARIFY, questions=questions, mentions=mentions), []
    if status in (UNSUPPORTED, NOT_AN_EVENT):
        message = _text(draft.get("message")) or (
            "这个问题暂时回答不了"
            if status == UNSUPPORTED
            else "个股回看只支持在某一天发生的事件，这个条件是一段时间里持续成立的状态"
        )
        return PlanResult(
            status, message=message, alternatives=clean_alternatives(draft.get("alternatives"))
        ), []
    return _read_ok(draft, context, events, mentions)


def _read_ok(
    draft: Mapping[str, Any],
    context: PlanContext,
    events: EventLibrary,
    mentions: tuple[Mention, ...],
) -> tuple[PlanResult, list[str]]:
    shape = _text(draft.get("shape"))
    spec: dict[str, Any] = {"shape": shape}
    problems: list[str] = []
    stock = board = None

    if shape in ("stock_list", "board_list"):
        target = "stock"
        if shape == "board_list":
            target = _text(draft.get("board_type"))
            if target not in ("sw_industry", "concept"):
                problems.append("板块表要填 board_type：sw_industry 或 concept")
            elif target not in context.targets:
                problems.append("概念板块当前不可用，board_type 只能是 sw_industry")
            spec["board_type"] = target
        _copy(draft, spec, "as_of")
        if expr := _text(draft.get("filter_expr")):
            spec["filter"] = {"expr": expr, "label": _text(draft.get("filter_label"))}
            problems += _expression_problems("filter_expr", expr, target, "filter")
        if by := _text(draft.get("sort_by")):
            order = _text(draft.get("sort_order")) or "desc"
            spec["sort"] = {"by": by, "order": order, "label": _text(draft.get("sort_label"))}
            problems += _expression_problems("sort_by", by, target, "sort")
        _copy(draft, spec, "limit")
        if shape == "stock_list":
            universe: dict[str, Any] = {}
            if base := _text(draft.get("universe_base")):
                universe["base"] = base
            if industry := _text(draft.get("industry")):
                if industry not in context.industries:
                    problems.append(f"industry 要从申万一级行业清单里选，收到「{industry}」")
                universe["industry"] = industry
            if universe:
                spec["universe"] = universe
            if mention := _text(draft.get("board_mention")):
                if "concept" not in context.targets:
                    problems.append("概念板块当前不可用，不要填 board_mention")
                board = NameMention(mention, _text(draft.get("board_guess")) or None)

    elif shape == "stock_history":
        if mention := _text(draft.get("stock_mention")):
            stock = NameMention(mention, _text(draft.get("stock_guess")) or None)
        else:
            problems.append("个股回看要填 stock_mention（用户原话里说的股票）")
        event_id, params = _text(draft.get("event_id")), draft.get("event_params") or {}
        if not event_id:
            problems.append(
                "个股回看要填 event_id，只能从事件库里选；条件不是事件时 status 填 not_an_event"
            )
        elif not isinstance(params, Mapping):
            problems.append("event_params 要是一个对象")
        else:
            try:
                render_event(event_id, params, events)
            except EventParamError as exc:
                problems.append(str(exc))
            spec["event"] = {"preset_id": event_id, "params": dict(params)}
        start, end = _text(draft.get("time_from")), _text(draft.get("time_to"))
        if start or end:
            spec["time_range"] = {
                "from": start or context.history_from.isoformat(),
                "to": end or context.latest_trading_day.isoformat(),
            }
        for key in ("horizons", "benchmark", "cost_bps"):
            _copy(draft, spec, key)
    else:
        return PlanResult(FAILED), [
            "status=ok 时 shape 要填 stock_list、board_list 或 stock_history"
        ]

    if not problems:
        problems = _structure_problems(spec, context, events)
    return PlanResult(OK, spec=spec, stock=stock, board=board, mentions=mentions), problems


def _structure_problems(
    spec: dict[str, Any], context: PlanContext, events: EventLibrary
) -> list[str]:
    """用 spec.parse_spec 查结构。股票代码、日期这些由 api 补的栏目先填个占位，只查大模型填的部分。"""
    trial = dict(spec)
    if spec["shape"] == "stock_history":
        trial["target"] = {"code": "000001.SZ"}
        trial.setdefault(
            "time_range",
            {
                "from": context.history_from.isoformat(),
                "to": context.latest_trading_day.isoformat(),
            },
        )
        event = spec["event"]
        rendered = render_event(event["preset_id"], event["params"], events)
        trial["event"] = {**event, "expr": rendered.expr}
    else:
        trial.setdefault("as_of", context.latest_trading_day.isoformat())
    try:
        parse_spec(trial)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(part) for part in error['loc'][1:]) or 'shape'}：{error['msg']}"
            for error in exc.errors()
        ]
    return []


def _expression_problems(name: str, text: str, target: str, purpose: str) -> list[str]:
    try:
        node = parse(text)
    except ExprSyntaxError as exc:
        return [f"{name}「{text}」写法不对：{exc}"]
    return [f"{name}「{text}」：{issue}" for issue in validate(node, target, purpose).issues]


def clean_alternatives(raw: object) -> tuple[str, ...]:
    """改写建议最多留 3 句：太长的、带百分比的、含买卖建议字眼的丢掉。"""
    kept: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = _text(item)
        if (
            not text
            or len(text) > MAX_ALTERNATIVE_LENGTH
            or _PERCENT.search(text)
            or any(word in text for word in _BANNED)
        ):
            continue
        if text not in kept:
            kept.append(text)
    return tuple(kept[:3])


def _questions(raw: object) -> tuple[Question, ...]:
    questions = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        text = _text(item.get("question"))
        options = item.get("options")
        choices = [_text(option) for option in options] if isinstance(options, list) else []
        choices = [choice for choice in choices if choice]
        if text and len(choices) >= 2:
            questions.append(Question(text, tuple(choices[:3])))
    return tuple(questions[:3])


def _mentions(raw: object) -> tuple[Mention, ...]:
    found: list[Mention] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        phrase, field = _text(item.get("phrase")), _text(item.get("field"))
        mention = Mention(phrase, field)
        if phrase and field in MENTION_FIELDS and mention not in found:
            found.append(mention)
    return tuple(found)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _copy(draft: Mapping[str, Any], spec: dict[str, Any], key: str) -> None:
    if draft.get(key) not in (None, "", []):
        spec[key] = draft[key]


# ── 提示词的变量 ────────────────────────────────────────────────


def system_variables(context: PlanContext, events: EventLibrary) -> dict[str, str]:
    """planner.system 的变量：清单全部由代码生成，大模型看到的和代码执行的是同一份（§5.5）。"""
    return {
        "today": context.today.isoformat(),
        "latest_trading_day": context.latest_trading_day.isoformat(),
        "history_from": context.history_from.isoformat(),
        "stock_fields": _fields("stock"),
        "board_fields": _board_fields(context),
        "operators": "\n".join(f"- {op['signature']}：{op['label']}" for op in operator_catalog()),
        "events": _events(events),
        "industries": "、".join(context.industries),
        "defaults": _defaults(context),
        "dates": _date_table(context),
    }


def _fields(target: str) -> str:
    lines = []
    for field in field_catalog(target):
        unit = f"（{field['unit']}）" if field["unit"] and field["unit"] != "布尔" else ""
        kind = "，条件" if field["type"] == "条件" else ""
        note = f"：{field['note']}" if field["note"] else ""
        timeseries = "（不能放进时序算子）" if not field["time_series_ok"] else ""
        lines.append(f"- {field['name']} {field['label']}{unit}{kind}{note}{timeseries}")
    return "\n".join(lines)


def _board_fields(context: PlanContext) -> str:
    parts = [f"申万一级行业（board_type=sw_industry）：\n{_fields('sw_industry')}"]
    if "concept" in context.targets:
        parts.append(f"通达信概念板块（board_type=concept）：\n{_fields('concept')}")
    else:
        parts.append("通达信概念板块当前不可用：不要用 board_type=concept，也不要填 board_mention")
    return "\n\n".join(parts)


def _events(events: EventLibrary) -> str:
    lines = []
    for event in event_catalog(events):
        params = "；".join(
            f"{p['name']} {p['label']}（可选 {p['allowed']}，默认 {p['default']}）"
            for p in event["params"]
        )
        example = json.dumps(event["example"]["params"], ensure_ascii=False)
        constraints = "；".join(event["constraints"])
        lines.append(
            f"- {event['id']} {event['name']}：{event['label']}"
            + (f"；参数：{params}" if params else "；没有参数")
            + (f"；{constraints}" if constraints else "")
            + f"。例：「{event['example']['question']}」→ event_params {example}"
        )
    return "\n".join(lines)


def _defaults(context: PlanContext) -> str:
    horizons = "、".join(str(h) for h in DEFAULTS["horizons"])  # type: ignore[attr-defined]
    return "\n".join(
        [
            f"- 股票表、板块表的日期：最近已收盘交易日 {context.latest_trading_day}",
            f"- 取前几名：{DEFAULTS['top_n']}；没有指定排序时按成交额从高到低",
            "- 股票池：沪深A股（不含北交所），剔除 ST、停牌、上市不满 60 个交易日",
            f"- 个股回看的回看区间：本地全部数据（{context.history_from} ~ {context.latest_trading_day}）",
            f"- 持有天数：{horizons} 个交易日",
            "- 同期对照：买入日全A等权平均",
            f"- 交易成本：{DEFAULTS['cost_bps']} 基点，买卖双边合计",
            f"- 「放量」：成交额超过前 20 日均额的 {DEFAULTS['volume_surge_ratio']:g} 倍；"
            f"「缩量」：{DEFAULTS['volume_shrink_ratio']:g} 倍",
            "- 「成交量」理解为成交额 $amount",
        ]
    )


_WEEKDAYS = "一二三四五六日"


def _date_table(context: PlanContext) -> str:
    """日期换算：今天星期几、最近 10 个交易日、本周 / 本月 / 本季度 / 今年以来各几个交易日。

    2026-09-15 实测：不给这张表，问「今年以来涨幅最大的 50 只」，大模型在思考里逐月数工作日、猜节假日，
    8000 个 token 用完也没给出结果（215 秒）；它估的 171 天，本地日历是 170 天。
    区间从今天所在的周、月、季、年算起；本地数据还没到这段时写明先同步，不让大模型拿旧数据凑。
    """
    today, latest = context.today, context.latest_trading_day
    lines = [
        f"- 今天：{today}（星期{_WEEKDAYS[today.weekday()]}）；"
        f"本地数据最近一个交易日：{latest}（星期{_WEEKDAYS[latest.weekday()]}）"
    ]
    days = sorted(day for day in context.trading_days if day <= latest)
    if not days:
        return "\n".join(lines)
    recent = "、".join(f"{day}（{_WEEKDAYS[day.weekday()]}）" for day in reversed(days[-10:]))
    lines.append(f"- 最近 10 个交易日，从近到远：{recent}")
    lines.append(
        "- 从某天起算的涨跌写成 Pct($close, N)：和起点前一个交易日的收盘价比，"
        f"N 是起点到 {latest} 的交易日数"
    )
    starts = {
        "本周以来": today - timedelta(days=today.weekday()),
        "本月以来": today.replace(day=1),
        "本季度以来": date(today.year, (today.month - 1) // 3 * 3 + 1, 1),
        "今年以来": date(today.year, 1, 1),
    }
    for label, start in starts.items():
        before = [day for day in days if day < start]
        if not before:
            continue  # 本地日历不够早，数不出来
        count = sum(day >= start for day in days)
        if count:
            lines.append(f"  - {label}：N={count}（比 {before[-1]} 收盘）")
        else:
            lines.append(f"  - {label}：本地还没有这段的数据（数据截至 {latest}），要先同步")
    return "\n".join(lines)


def _questions_text(questions: tuple[Question, ...]) -> str:
    return "\n".join(f"- {q.question}（选项：{'、'.join(q.options)}）" for q in questions)
