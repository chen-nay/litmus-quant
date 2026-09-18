"""llm.plan()：中文提问 → 查询条件草稿（ARCHITECTURE §5.2、DESIGN.md §4）。

- 大模型填的格式和 QuerySpec 同一个样子（五个维度，嵌套），只有两处不同：点名的标的、限定的板块
  只填原话和猜的名字（mentions / board.mention），代码由 api 用 ds.resolve_* 查（§2.3）。
  嵌套格式 2026-09-17 在火山引擎 glm-5.3-flash 上实测通过
- 一次调用就分出去向：status 分出改写建议、澄清卡，output.kind 分出表、卡、统计（DESIGN.md §5）
- 大模型只填用户说到的栏目，没说的由代码补默认值并在确认卡上标出来（§5.3、§5.4）
- 防线②：表达式过 expr.parse + validate，事件编号和参数过 signals.render_event，结构过 spec.parse_spec，
  再加几条实测抓到的毛病（卡带了排序、猜的名字填成代码）。不过就带着问题清单用 planner.repair 重试一次，
  还不过返回 failed
- 实测大模型偶尔漏填 status：有问题清单就当澄清，填了 output 就当 ok
- 改写建议只留几句像样的：条数、长度、禁用词、带百分比的丢掉（§5.2）
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError

from litmus.expr import ExprSyntaxError, field_catalog, operator_catalog, parse, validate
from litmus.llm.client import LLMClient, LLMError, LLMFormatError
from litmus.llm.models import (
    CLARIFY,
    FAILED,
    NOT_AN_EVENT,
    OK,
    UNSUPPORTED,
    LLMCall,
    NameMention,
    PlanContext,
    PlanResult,
    PreviousTurn,
    Question,
)
from litmus.llm.prompts import load_prompt
from litmus.signals import EventLibrary, EventParamError, event_catalog, render_event
from litmus.spec import (
    BENCHMARKS,
    CARD_BENCHMARKS,
    DEFAULT_CARD_METRICS,
    DEFAULTS,
    TARGETS,
    Mention,
    parse_spec,
)

logger = logging.getLogger(__name__)

#: 原话里的说法能对应的栏目（确认卡按这些栏目写「理解为」）。指标写成 metrics.<指标名>
MENTION_FIELDS = (
    "when.as_of",
    "when.range",
    "scope.target",
    "scope.base",
    "scope.industry",
    "scope.board",
    "scope.exclude",
    "subject",
    "output.filter",
    "output.sort",
    "output.limit",
    "output.event",
    "output.horizons",
    "output.benchmark",
    "output.cost_bps",
)

_STR: dict[str, Any] = {"type": "string"}
_DAY: dict[str, Any] = {"type": "string", "description": "YYYY-MM-DD"}
_KINDS = ("table", "card", "event_study")

#: 用户原话里说的一个标的
_NAMED: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mention": {
            "type": "string",
            "description": "用户原话里的说法，如「茅台」「宁王」「半导体」",
        },
        "guess": {
            "type": "string",
            "description": "你猜的全称，如「贵州茅台」「宁德时代」。不是代码，不要写 600519 这类数字",
        },
    },
    "required": ["mention"],
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": [OK, CLARIFY, UNSUPPORTED, NOT_AN_EVENT]},
        "scope": {
            "type": "object",
            "description": "在谁身上算：排名在这里面排。用户没说就整个不填",
            "properties": {
                "target": {"type": "string", "enum": list(TARGETS)},
                "base": {"type": "string", "enum": ["all_a", "hs300", "zz500"]},
                "board": {
                    "type": "object",
                    "description": "限定在某个行业、板块、概念里",
                    "properties": {
                        "mention": {
                            "type": "string",
                            "description": "用户原话，如「半导体板块」「银行股」",
                        },
                        "guess": {
                            "type": "string",
                            "description": "猜的申万行业名或通达信概念板块名，拿不准写 2~3 个，用「、」隔开",
                        },
                        "code": {
                            "type": "string",
                            "description": "只在修改现有条件、概念板块没换时填：照抄 scope.board.code",
                        },
                    },
                },
                "industry": {
                    "type": "string",
                    "description": "只在修改现有条件、行业没换时填：照抄 scope.industry",
                },
                "exclude": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(DEFAULTS["exclude"])},  # type: ignore[call-overload]
                    "description": "只在用户要看 ST 股或次新股时填：写还要剔除的那几项。"
                    '看次新股填 ["ST", "suspended"]，看 ST 股填 ["suspended", "new_listing_60d"]',
                },
            },
        },
        "subject": {
            "type": "object",
            "description": "最后看谁：pool 整个范围（表），codes 点名看某几个（卡、统计）",
            "properties": {
                "kind": {"type": "string", "enum": ["pool", "codes"]},
                "mentions": {"type": "array", "items": _NAMED},
                "codes": {
                    "type": "array",
                    "items": _STR,
                    "description": "只在修改现有条件、看的对象没换时填：照抄 subject.codes",
                },
            },
        },
        "when": {
            "type": "object",
            "description": "用户没说日期就整个不填",
            "properties": {
                "as_of": _DAY,
                "range": {"type": "object", "properties": {"from": _DAY, "to": _DAY}},
            },
        },
        "metrics": {
            "type": "array",
            "description": "算哪些数：一项一个名字和一个公式",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "几个字的中文名，也是 sort.by 引用它的钥匙",
                    },
                    "expr": {"type": "string", "description": "公式，只能用给定的字段和算子"},
                },
                "required": ["name", "expr"],
            },
        },
        "output": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_KINDS)},
                "filter": {"type": "object", "properties": {"expr": _STR, "label": _STR}},
                "sort": {
                    "type": "object",
                    "properties": {
                        "by": {
                            "type": "string",
                            "description": "metrics 里某一项的 name，不是公式",
                        },
                        "order": {"type": "string", "enum": ["asc", "desc"]},
                    },
                },
                "limit": {"type": "integer"},
                "event": {
                    "type": "object",
                    "properties": {
                        "preset_id": _STR,
                        "params": {"type": "object", "additionalProperties": {"type": "number"}},
                    },
                },
                "horizons": {"type": "array", "items": {"type": "integer"}},
                "benchmark": {
                    "type": "string",
                    "enum": list(BENCHMARKS),
                    "description": "统计的同期对照；卡只在要和沪深300 / 中证500 比时填 index:…",
                },
                "cost_bps": {"type": "number"},
            },
            "required": ["kind"],
        },
        "narrate": {
            "type": "boolean",
            "description": "卡下面再写一段话。只在卡上、用户要一个判断或概括时填 true",
        },
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": _STR,
                    "field": {
                        "type": "string",
                        "description": "栏目："
                        + "、".join(MENTION_FIELDS)
                        + "，指标写 metrics.<指标名>",
                    },
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
    revise: Mapping[str, Any] | None = None,
) -> PlanResult:
    """query：用户这次说的话。

    - previous：上一轮追问，query 是对追问的回答
    - revise：确认卡上现在的条件，query 是「要改哪里」。大模型在这份条件上改，没说到的栏目照抄，
      股票和概念板块照抄代码（stock_code / board_code）——之前选过的候选、表单上改过的不会丢
    """
    system_prompt = load_prompt("planner.system")
    system = system_prompt.render(**system_variables(context, events))
    user = query
    if revise is not None:
        user = load_prompt("planner.revise").render(
            spec=json.dumps(revise, ensure_ascii=False, indent=1), change=query
        )
    elif previous is not None:
        user = load_prompt("planner.followup").render(
            query=previous.query, questions=_questions_text(previous.questions), answer=query
        )
    version = system_prompt.version
    rendered = hashlib.sha256(system.encode("utf-8")).hexdigest()[:8]
    message, draft, problems = user, {}, []
    # 每次调用都记一条，重试就有两条：调了什么、回了什么、哪里没通过检查（存进 store 的过程记录）
    calls: list[LLMCall] = []

    def record(**extra: object) -> LLMCall:
        call = LLMCall(
            attempt=attempt,
            prompt_id=system_prompt.id,
            prompt_version=version,
            rendered_hash=rendered,
            user_message=message,
            **extra,  # type: ignore[arg-type]
        )
        calls.append(call)
        return call

    for attempt in (1, 2):
        if attempt == 2 and problems:
            message = load_prompt("planner.repair").render(
                query=user,
                previous_output=json.dumps(draft, ensure_ascii=False, indent=1),
                problems="\n".join(f"- {problem}" for problem in problems),
            )
        try:
            reply = client.structured(system, message, OUTPUT_SCHEMA)
        except LLMFormatError as exc:
            # 没调用工具就没有输出可改，原样再问一次
            logger.warning("planner.system@%s 第 %d 次没按格式返回：%s", version, attempt, exc)
            record(error=str(exc))
            problems = []
            if attempt == 1:
                continue
            return PlanResult(
                FAILED, attempts=attempt, prompt_version=version, error=str(exc), calls=tuple(calls)
            )
        except LLMError as exc:
            logger.warning("planner.system@%s 第 %d 次调用失败：%s", version, attempt, exc)
            record(error=str(exc))
            return PlanResult(
                FAILED,
                attempts=attempt,
                prompt_version=version,
                error=str(exc),
                calls=tuple(calls),
            )
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
        record(
            model=reply.model,
            raw_reply=dict(draft),
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            seconds=round(reply.seconds, 3),
            problems=tuple(problems),
        )
        if not problems:
            return replace(result, attempts=attempt, prompt_version=version, calls=tuple(calls))
        logger.info("planner.system@%s 第 %d 次输出没通过检查：%s", version, attempt, problems)
    return PlanResult(
        FAILED, attempts=2, prompt_version=version, error="；".join(problems), calls=tuple(calls)
    )


# ── 大模型的输出 → 结果 ─────────────────────────────────────────


def read_output(
    draft: Mapping[str, Any], context: PlanContext, events: EventLibrary
) -> tuple[PlanResult, list[str]]:
    """返回 (结果, 问题清单)。问题清单非空时结果不能用，要带着问题重试。"""
    questions = _questions(draft.get("questions"))
    status = _text(draft.get("status"))
    if not status:
        status = CLARIFY if questions else (OK if isinstance(draft.get("output"), Mapping) else "")
    if status not in (OK, CLARIFY, UNSUPPORTED, NOT_AN_EVENT):
        allowed = "ok、needs_clarification、unsupported、not_an_event"
        return PlanResult(FAILED), [f"status 只能是 {allowed}，收到 {draft.get('status')!r}"]
    mentions = _mentions(draft.get("mentions"), _metric_names(draft))
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
    """大模型的格式 → QuerySpec 的草稿，顺手查出问题。

    格式和 QuerySpec 一样，这里只做三件事：把点名的标的、限定的板块拿出来交给 api 去查代码；
    查表达式、事件；查几条实测抓到的毛病。结构对不对最后交给 spec.parse_spec。
    """
    problems: list[str] = []
    scope_in, subject_in = _object(draft.get("scope")), _object(draft.get("subject"))
    output_in = _object(draft.get("output"))
    kind = _text(output_in.get("kind"))
    if kind not in _KINDS:
        return PlanResult(FAILED), [f"status=ok 时 output.kind 要填 {'、'.join(_KINDS)}"]

    target = _text(scope_in.get("target")) or "stock"
    if target not in TARGETS:
        problems.append(f"scope.target 只能是 {'、'.join(TARGETS)}")
    elif target not in context.targets:
        usable = "、".join(t for t in TARGETS if t in context.targets)
        problems.append(f"{_TARGET_LABELS[target]}当前不可用，scope.target 只能是 {usable}")
    scope: dict[str, Any] = {}
    if _text(scope_in.get("target")):
        scope["target"] = target
    if base := _text(scope_in.get("base")):
        scope["base"] = base
    if isinstance(scope_in.get("exclude"), list):
        scope["exclude"] = [_text(item) for item in scope_in["exclude"] if _text(item)]
    if industry := _text(scope_in.get("industry")):
        if industry not in _industry_names(context):
            problems.append(f"scope.industry 要照抄现有条件里的申万行业名，收到「{industry}」")
        scope["industry"] = industry
    board = _board(_object(scope_in.get("board")))
    if board is not None and target != "stock":
        problems.append("按板块排行、比较时（scope.target 是板块）不要再填 scope.board")

    subject_kind = _text(subject_in.get("kind")) or ("pool" if kind == "table" else "codes")
    subject: dict[str, Any] = {"kind": subject_kind}
    subjects: tuple[NameMention, ...] = ()
    if subject_kind == "codes":
        subjects, subject_problems = _subjects(subject_in)
        problems += subject_problems
    if subject_kind == "codes" and not subjects:
        problems.append("点名看谁时 subject.mentions 要填用户原话里的股票或板块")
    if kind == "event_study" and target != "stock":
        problems.append("事件统计只能看股票：scope.target 不填")

    metrics = _metrics(draft.get("metrics"))
    for metric in metrics:
        problems += _expression_problems(
            f"指标「{metric['name']}」", metric["expr"], target, "metric"
        )
    output, output_problems = _output(output_in, kind, metrics, target, events)
    problems += output_problems

    spec: dict[str, Any] = {"subject": subject, "output": output}
    if scope:
        spec["scope"] = scope
    if metrics:
        spec["metrics"] = metrics
    when = _when(_object(draft.get("when")), context)
    if when:
        spec["when"] = when
    if draft.get("narrate") is True:
        spec["narrate"] = True
    if not problems:
        problems = _structure_problems(spec, context, events)
    result = PlanResult(OK, spec=spec, subjects=subjects, board=board, mentions=mentions)
    return result, problems


def _output(
    raw: Mapping[str, Any],
    kind: str,
    metrics: list[dict[str, str]],
    target: str,
    events: EventLibrary,
) -> tuple[dict[str, Any], list[str]]:
    output: dict[str, Any] = {"kind": kind}
    problems: list[str] = []
    names = [metric["name"] for metric in metrics]
    if kind == "card":
        extra = [key for key in ("filter", "sort", "limit") if raw.get(key) not in (None, {}, "")]
        if extra:
            problems.append(f"卡不筛不排，output 里不要填 {'、'.join(extra)}")
        if benchmark := _text(raw.get("benchmark")):
            if benchmark not in CARD_BENCHMARKS:
                indexes = "、".join(b for b in CARD_BENCHMARKS if b.startswith("index:"))
                problems.append(f"卡的 output.benchmark 只能是 {indexes}，不和指数比就不填")
            output["benchmark"] = benchmark
        return output, problems
    if kind == "table":
        condition = _object(raw.get("filter"))
        if expr := _text(condition.get("expr")):
            output["filter"] = {"expr": expr, "label": _text(condition.get("label"))}
            problems += _expression_problems("筛选条件", expr, target, "filter")
        sort = _object(raw.get("sort"))
        if by := _text(sort.get("by")):
            output["sort"] = {"by": by, "order": _text(sort.get("order")) or "desc"}
            if by not in names:
                listed = "、".join(names) if names else "（还没有指标）"
                problems.append(
                    f"sort.by 要填 metrics 里某一项的 name，不是公式：收到「{by}」，可选：{listed}。"
                    "按一个新的数排序，先把它加进 metrics"
                )
            else:
                expr = metrics[names.index(by)]["expr"]
                problems += _expression_problems(f"排序用的指标「{by}」", expr, target, "sort")
        _copy(raw, output, "limit")
        return output, problems
    event = _object(raw.get("event"))
    event_id, params = _text(event.get("preset_id")), event.get("params") or {}
    if not event_id:
        problems.append(
            "事件统计要填 output.event.preset_id，只能从事件库里选；条件不是事件时 status 填 not_an_event"
        )
    elif not isinstance(params, Mapping):
        problems.append("output.event.params 要是一个对象")
    else:
        try:
            render_event(event_id, params, events)
        except EventParamError as exc:
            problems.append(str(exc))
        output["event"] = {"preset_id": event_id, "params": dict(params)}
    for key in ("horizons", "benchmark", "cost_bps"):
        _copy(raw, output, key)
    return output, problems


def _board(raw: Mapping[str, Any]) -> NameMention | None:
    """限定的行业、板块：改现有条件时照抄的代码，或者原话加猜的名字。"""
    if code := _text(raw.get("code")):
        return NameMention(code, is_code=True)
    if mention := _text(raw.get("mention")):
        return NameMention(mention, _text(raw.get("guess")) or None)
    return None


#: 猜的名字填成了代码：002714、002714.SZ（2026-09-17 实测）
_LOOKS_LIKE_CODE = re.compile(r"^\d{6}(\.[A-Za-z]{2})?$")


def _subjects(raw: Mapping[str, Any]) -> tuple[tuple[NameMention, ...], list[str]]:
    """点名看的标的：改现有条件时照抄的代码在前，新说的在后。"""
    found: list[NameMention] = []
    problems: list[str] = []
    for code in raw.get("codes") if isinstance(raw.get("codes"), list) else []:
        if text := _text(code):
            found.append(NameMention(text, is_code=True))
    for item in raw.get("mentions") if isinstance(raw.get("mentions"), list) else []:
        entry = _object(item)
        mention, guess = _text(entry.get("mention")), _text(entry.get("guess"))
        if not mention:
            continue
        if _LOOKS_LIKE_CODE.match(guess):
            problems.append(f"「{mention}」的 guess 要填你猜的全称，不要填代码（收到 {guess}）")
        found.append(NameMention(mention, guess or None))
    unique = tuple(dict.fromkeys(found))
    return unique, problems


def _metrics(raw: object) -> list[dict[str, str]]:
    metrics = []
    for item in raw if isinstance(raw, list) else []:
        entry = _object(item)
        name, expr = _text(entry.get("name")), _text(entry.get("expr"))
        if name and expr:
            metrics.append({"name": name, "expr": expr})
    return metrics


def _metric_names(draft: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(metric["name"] for metric in _metrics(draft.get("metrics")))


def _when(raw: Mapping[str, Any], context: PlanContext) -> dict[str, Any]:
    """只说了一头的区间（「2020 年以来」）另一头用本地数据的起点、终点。"""
    if as_of := _text(raw.get("as_of")):
        return {"as_of": as_of}
    span = _object(raw.get("range"))
    first, last = _text(span.get("from")), _text(span.get("to"))
    if first or last:
        full = _full_range(context)
        return {"range": {"from": first or full["from"], "to": last or full["to"]}}
    return {}


def _full_range(context: PlanContext) -> dict[str, str]:
    return {"from": context.history_from.isoformat(), "to": context.latest_trading_day.isoformat()}


def _object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _structure_problems(
    spec: dict[str, Any], context: PlanContext, events: EventLibrary
) -> list[str]:
    """用 spec.parse_spec 查结构。代码、日期这些由 api 补的栏目先填个占位，只查大模型填的部分。"""
    trial = {key: dict(value) if isinstance(value, dict) else value for key, value in spec.items()}
    output = dict(trial["output"])
    if trial["subject"].get("kind") == "codes":
        trial["subject"] = {"kind": "codes", "codes": ["000001.SZ"]}
    if output["kind"] == "event_study":
        trial.setdefault("when", {"range": _full_range(context)})
        event = output["event"]
        rendered = render_event(event["preset_id"], event["params"], events)
        output["event"] = {**event, "expr": rendered.expr}
    else:
        trial.setdefault("when", {"as_of": context.latest_trading_day.isoformat()})
    trial["output"] = output
    try:
        parse_spec(trial)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(part) for part in error['loc'] if str(part) not in _KINDS) or 'output'}"
            f"：{error['msg']}"
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


def _mentions(raw: object, metric_names: tuple[str, ...] = ()) -> tuple[Mention, ...]:
    """原话的说法挂在哪一栏。栏目不认识的丢掉；指标只认 metrics.<这次的指标名>。"""
    fields = {*MENTION_FIELDS, *(f"metrics.{name}" for name in metric_names)}
    found: list[Mention] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        phrase, field = _text(item.get("phrase")), _text(item.get("field"))
        mention = Mention(phrase, field)
        if phrase and field in fields and mention not in found:
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
        "industries": _industries(context),
        "defaults": _defaults(context),
        "default_metrics": _default_metrics(context),
        "since_new_year": _since_new_year(context),
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
    parts = [f"申万一级行业（scope.target=sw_industry）：\n{_fields('sw_industry')}"]
    if "sw_industry_l2" in context.targets:
        parts.append("申万二级行业（scope.target=sw_industry_l2）：字段和一级一样")
    else:
        parts.append("申万二级行业当前不可用：scope.target 不要填 sw_industry_l2")
    if "concept" in context.targets:
        parts.append(f"通达信概念板块（scope.target=concept）：\n{_fields('concept')}")
    else:
        parts.append(
            "通达信概念板块当前不可用：scope.target 不要填 concept，scope.board 也只能是申万行业"
        )
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
    small_cap = f"{DEFAULTS['small_cap'] / 1e8:g}亿"  # type: ignore[operator]
    return "\n".join(
        [
            f"- 表和卡的日期：最近已收盘交易日 {context.latest_trading_day}",
            f"- 表取前几名：{DEFAULTS['top_n']}；没有指定排序时按成交额从高到低",
            "- 算的范围：沪深A股（不含北交所），剔除 ST、停牌、上市不满 60 个交易日",
            f"- 事件统计的回看区间：本地全部数据（{context.history_from} ~ {context.latest_trading_day}）",
            f"- 持有天数：{horizons} 个交易日",
            "- 同期对照：买入日算的范围（默认全A）等权平均",
            f"- 交易成本：{DEFAULTS['cost_bps']} 基点，买卖双边合计",
            f"- 「放量」：成交额超过前 20 日均额的 {DEFAULTS['volume_surge_ratio']:g} 倍；"
            f"「缩量」：{DEFAULTS['volume_shrink_ratio']:g} 倍",
            "- 「成交量」理解为成交额 $amount",
            "- 「涨幅」完全没提时间：当日涨跌幅 $pct_chg，最近一个交易日",
            f"- 「小市值」没给数字：总市值低于 {small_cap}，写成 $market_cap < {small_cap}",
        ]
    )


def _since_new_year(context: PlanContext) -> str:
    """「今年以来」的 PctSince 起点：去年最后一个交易日。本地日历数不到时退回 12 月 31 日。"""
    first_day = date(context.today.year, 1, 1)
    before = [day for day in context.trading_days if day < first_day]
    return (before[-1] if before else first_day - timedelta(days=1)).strftime("%Y%m%d")


def _default_metrics(context: PlanContext) -> str:
    anchor = _since_new_year(context)
    return "\n".join(
        f"- {name}：{expr.format(since_new_year=anchor)}" for name, expr in DEFAULT_CARD_METRICS
    )


#: 标的类型的叫法
_TARGET_LABELS = {
    "stock": "股票",
    "sw_industry": "申万一级行业",
    "sw_industry_l2": "申万二级行业",
    "concept": "通达信概念板块",
}


def _industry_names(context: PlanContext) -> tuple[str, ...]:
    return (*context.industries, *(name for _, name in context.industries_l2))


def _industries(context: PlanContext) -> str:
    """申万行业清单：一级一行，二级按所属一级分组。"""
    lines = [f"一级（{len(context.industries)} 个）：{'、'.join(context.industries)}"]
    if not context.industries_l2:
        lines.append("二级行业当前不可用")
        return "\n".join(lines)
    groups: dict[str, list[str]] = {}
    for parent, name in context.industries_l2:
        groups.setdefault(parent, []).append(name)
    lines.append(f"二级（{len(context.industries_l2)} 个，按所属一级分组）：")
    lines.extend(f"- {parent}：{'、'.join(names)}" for parent, names in groups.items())
    return "\n".join(lines)


_WEEKDAYS = "一二三四五六日"


def _date_table(context: PlanContext) -> str:
    """日期换算：今天星期几、最近 10 个交易日、本周 / 本月 / 本季度 / 今年以来各几个交易日。

    2026-09-15 实测：不给这张表，问「今年以来涨幅最大的 50 只」，大模型在思考里逐月数工作日、猜节假日，
    8000 个 token 用完也没给出结果（215 秒）；它估的 171 天，本地日历是 170 天。
    区间从今天所在的周、月、季、年算起；本地数据还没到这段时写明本地数据截至哪天，不让大模型拿旧数据凑。
    不说「要先同步」：可能是当天行情还没发布，同步也补不到（2026-09-15 定：数据旧了不特殊处理）。
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
    lines.append("- 从某天起算的涨跌写成 PctSince($close, 起始日前一个交易日)，按日期取值：")
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
            anchor = before[-1].strftime("%Y%m%d")
            lines.append(
                f"  - {label}：PctSince($close, {anchor})（和 {before[-1]} 收盘比，到 {latest} 共 {count} 个交易日）"
            )
        else:
            lines.append(f"  - {label}：本地数据截至 {latest}，还没有这段的行情")
    return "\n".join(lines)


def _questions_text(questions: tuple[Question, ...]) -> str:
    return "\n".join(f"- {q.question}（选项：{'、'.join(q.options)}）" for q in questions)
