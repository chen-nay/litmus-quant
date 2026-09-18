"""/api/run 的确定性检查（ARCHITECTURE §1.4 ②）：任何一条不过就返回 needs_revision，绝不执行。

按顺序查，前一类有问题就不往下查（结构都不对，谈不上查表达式和数据）：

1. **事件**：事件统计只能用事件库里的事件。按「编号 + 参数」重新生成表达式和标签，请求里带来的表达式、标签不作数——
   用户在确认卡上改了参数也走这一步，不再调 LLM
2. **结构**：栏目齐不齐、类型对不对、数值在不在范围里、维度之间的组合合不合法（spec.parse_spec），说明翻成中文
3. **表达式**：写法和字段、算子白名单（expr.parse + expr.validate），标的类型当前可用
4. **日期**：没给日期时按**这个查询实际用到哪几类数据**回填——取它们最新日里最早的那个。
   概念板块常落后于股票行情，一刀切取股票的最新日，问概念板块就会落到没有数据的那天。
   要知道用到哪些数据得先看表达式里有哪些字段，所以这一步排在表达式检查之后
5. **数据**：日期是交易日、落在本地数据范围里；股票代码、申万行业名、概念板块代码本地查得到；
   点名看的标的在不在算的范围里
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from pydantic import ValidationError

from litmus.api.models import Issue
from litmus.data import CONCEPT, STOCK, SW_INDUSTRY, SW_INDUSTRY_L2, DataService
from litmus.expr import ExprSyntaxError, parse, validate
from litmus.research import pool_of
from litmus.research.table import DEFAULT_SORT_EXPR, DEFAULT_SORT_NAME
from litmus.signals import EventLibrary, EventParamError, render_event
from litmus.spec import EventStudyOutput, QuerySpec, TableOutput, When, parse_spec

Spec = QuerySpec

#: output 的三种形态，pydantic 报错时会把形态名插进路径里
_OUTPUT_KINDS = ("table", "card", "event_study")
_TARGET_LABELS = {
    STOCK: "股票",
    SW_INDUSTRY: "申万行业",
    SW_INDUSTRY_L2: "申万二级行业",
    CONCEPT: "概念板块",
}
_UNAVAILABLE = "{}当前不可用，原因见 /api/data/status"

#: pydantic 的错误类型 → 中文说明。花括号里的由错误自带的上下文填
_MESSAGES = {
    "missing": "缺少这一项",
    "extra_forbidden": "不认识这一项",
    "literal_error": "只能是 {expected}",
    "greater_than_equal": "不能小于 {ge}",
    "greater_than": "要大于 {gt}",
    "less_than_equal": "不能大于 {le}",
    "less_than": "要小于 {lt}",
    "string_too_short": "不能为空",
    "string_type": "要是文字",
    "model_type": "要是一个对象",
    "dict_type": "要是一个对象",
    "model_attributes_type": "要是一个对象",
    "list_type": "要是一个列表",
    "tuple_type": "要是一个列表",
    "union_tag_invalid": "只能是 {expected_tags}",
    "union_tag_not_found": "缺少 kind，可选 'table'、'card'、'event_study'",
    "finite_number": "要是有限的数字",
}
_PREFIXES = (("date", "日期要写成 YYYY-MM-DD"), ("int", "要是整数"), ("float", "要是数字"))


def check_spec(
    raw: object, ds: DataService, events: EventLibrary
) -> tuple[Spec | None, list[Issue]]:
    """通过返回 (spec, [])，事件已经按事件库重新生成、日期已经回填；不通过返回 (None, 要改的地方)。"""
    if not isinstance(raw, dict):
        return None, [Issue(path="spec", message="spec 要是一个对象")]
    raw, event_issues, event_defaults = _render_event(raw, events)
    raw, sort_defaults = _fill_sort(raw)
    # 用了哪些默认值、说明文字都由代码定，请求里带来的不作数
    defaults = [*_missing_defaults(raw), *event_defaults, *sort_defaults]
    raw = {**raw, "defaults_used": defaults, "assumptions": []}
    try:
        spec = parse_spec(raw)
    except ValidationError as exc:
        structure = _structure_issues(exc)
        if event_issues:  # 事件已经报过了，别再连带报「output.event.expr 缺少这一项」
            structure = [i for i in structure if not _under(i.path, "output.event")]
        return None, _unique(event_issues + structure)
    if event_issues:
        return None, event_issues
    if issues := _expression_issues(spec, ds):
        return None, issues
    spec = _fill_when(spec, ds)
    return (None, issues) if (issues := _data_issues(spec, ds)) else (spec, [])


# ── 1. 事件 ─────────────────────────────────────────────────────


def _render_event(
    raw: dict[str, Any], events: EventLibrary
) -> tuple[dict[str, Any], list[Issue], list[str]]:
    """返回 (请求, 问题, 用了默认值的事件参数路径)。"""
    output = raw.get("output")
    if not isinstance(output, dict) or output.get("kind") != "event_study":
        return raw, [], []
    if not isinstance(output.get("event"), dict):
        return raw, [], []  # 结构检查会报
    event = output["event"]
    preset_id = event.get("preset_id")
    if not preset_id:
        message = "事件要从事件库里选，可选的见 /api/events（暂不支持自定义事件）"
        return raw, [Issue(path="output.event.preset_id", message=message)], []
    params = event.get("params") or {}
    if not isinstance(params, Mapping):
        return raw, [], []  # 结构检查会报 output.event.params
    try:
        rendered = render_event(str(preset_id), params, events)
    except EventParamError as exc:
        path = "output.event.preset_id" if exc.param is None else f"output.event.params.{exc.param}"
        return raw, [Issue(path=path, message=str(exc), allowed=exc.allowed)], []
    event = {
        **event,
        "preset_id": rendered.preset_id,
        "params": rendered.params,
        "expr": rendered.expr,
        "label": rendered.label,
        "library_version": rendered.library_version,
    }
    defaults = [f"output.event.params.{name}" for name in rendered.defaults_used]
    return {**raw, "output": {**output, "event": event}}, [], defaults


# ── 2. 默认值 ───────────────────────────────────────────────────

#: 请求里没给、由默认值补上的栏目（spec.DEFAULTS），确认卡据此标「默认值，可修改」。
#: 路径和 QuerySpec 的字段名一一对应，改了 spec 就要一起改——test_checks 里有一条比对
_DEFAULTABLE: dict[str, tuple[tuple[str, ...], ...]] = {
    "table": (
        ("when",),
        ("scope", "base"),
        ("scope", "exclude"),
        ("output", "limit"),
    ),
    "card": (("when",), ("scope", "base"), ("scope", "exclude")),
    "event_study": (
        ("when",),
        ("scope", "base"),
        ("scope", "exclude"),
        ("output", "horizons"),
        ("output", "benchmark"),
        ("output", "cost_bps"),
    ),
}


def _kind(raw: Mapping[str, Any]) -> str:
    output = raw.get("output")
    return str(output.get("kind")) if isinstance(output, Mapping) else ""


def _missing_defaults(raw: dict[str, Any]) -> list[str]:
    missing = []
    for path in _DEFAULTABLE.get(_kind(raw), ()):
        value: Any = raw
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        if value is None or value == {}:
            missing.append(".".join(path))
    return missing


def _fill_sort(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """表没给排序时按成交额排。`sort.by` 指的是一个 metric 的名字，所以要连 metric 一起补上。"""
    output = raw.get("output")
    if not isinstance(output, dict) or output.get("kind") != "table" or output.get("sort"):
        return raw, []
    metrics = list(raw.get("metrics") or [])
    if not any(isinstance(m, Mapping) and m.get("name") == DEFAULT_SORT_NAME for m in metrics):
        metrics.append({"name": DEFAULT_SORT_NAME, "expr": DEFAULT_SORT_EXPR})
    sort = {"by": DEFAULT_SORT_NAME, "order": "desc"}
    return {**raw, "metrics": metrics, "output": {**output, "sort": sort}}, ["output.sort"]


# ── 3. 结构 ─────────────────────────────────────────────────────


def _structure_issues(exc: ValidationError) -> list[Issue]:
    issues = []
    for error in exc.errors():
        loc = [str(part) for part in error["loc"]]
        # 按 kind 分派 output 时，pydantic 会插一个形态名
        loc = [part for part in loc if part not in _OUTPUT_KINDS]
        issues.append(Issue(path=".".join(loc) or None, message=_message(error)))
    return issues


def _message(error: Mapping[str, Any]) -> str:
    kind, context = error["type"], error.get("ctx") or {}
    if kind in ("value_error", "assertion_error"):  # 自己写的校验，本来就是中文
        return str(context.get("error", error["msg"]))
    if kind in _MESSAGES:
        values = {
            key: str(value).replace(" or ", "、").replace(", ", "、")
            for key, value in context.items()
        }
        return _MESSAGES[kind].format(**values)
    for prefix, message in _PREFIXES:
        if kind.startswith(prefix):
            return message
    return str(error["msg"])


# ── 4. 表达式 ───────────────────────────────────────────────────


def _expression_issues(spec: Spec, ds: DataService) -> list[Issue]:
    target = spec.scope.target
    if target not in ds.available_targets():
        return [Issue(path="scope.target", message=_UNAVAILABLE.format(_TARGET_LABELS[target]))]
    if isinstance(spec.output, EventStudyOutput):
        return []  # 事件表达式来自事件库，库加载时已经把全部参数组合校验过
    issues: list[Issue] = []
    exprs = {metric.name: metric.expr for metric in spec.metrics}
    for name, text in exprs.items():
        issues += _expr_issues(f"metrics.{name}", text, target, "metric")
    if isinstance(spec.output, TableOutput):
        if spec.output.filter is not None:
            issues += _expr_issues("output.filter.expr", spec.output.filter.expr, target, "filter")
        # 指标可以是「是 / 否」（卡上一行「站上年线：是」），但排序要按数值排
        if spec.output.sort is not None and (by := exprs.get(spec.output.sort.by)):
            issues += [
                Issue(path="output.sort.by", message=issue.message, position=issue.position)
                for issue in _expr_issues("output.sort.by", by, target, "sort")
            ]
    return issues


def _expr_issues(path: str, text: str, target: str, purpose: str) -> list[Issue]:
    try:
        node = parse(text)
    except ExprSyntaxError as exc:
        return [Issue(path=path, message=exc.message, position=exc.position)]
    return [
        Issue(path=path, message=item.message, position=item.position)
        for item in validate(node, target, purpose).issues
    ]


# ── 5. 日期 ─────────────────────────────────────────────────────


def _fill_when(spec: Spec, ds: DataService) -> Spec:
    """没给日期的补上。

    时点取**这个查询实际用到的几类数据**里最新日最早的那个：用了概念板块行情就不能取股票的最新日，
    概念板块常落后几天。区间取本地全部数据。
    """
    if spec.when.as_of is not None or spec.when.range is not None:
        return spec
    if isinstance(spec.output, EventStudyOutput):
        first, last = ds.data_range(STOCK)
        span = {"from": first.isoformat(), "to": last.isoformat()}
        return spec.model_copy(update={"when": When.model_validate({"range": span})})
    days = [ds.data_range(target)[1] for target in _targets_used(spec, ds)]
    return spec.model_copy(update={"when": When(as_of=min(days))}) if days else spec


def _targets_used(spec: Spec, ds: DataService) -> list[str]:
    """这个查询会读哪几类数据。标的本身一类，用到财务字段再算一类。"""
    used = {spec.scope.target}
    if spec.scope.board is not None and CONCEPT in ds.available_targets():
        used.add(CONCEPT)  # 概念板块成分跟着概念板块行情一起同步
    return [target for target in used if target in ds.available_targets()]


# ── 6. 数据 ─────────────────────────────────────────────────────


def _data_issues(spec: Spec, ds: DataService) -> list[Issue]:
    if isinstance(spec.output, EventStudyOutput):
        return _event_study_issues(spec, ds)
    target = spec.scope.target
    issues = []
    first, last = ds.data_range(target)
    as_of = spec.when.as_of
    if as_of is None:
        return [Issue(path="when.as_of", message="要给一个日期")]
    if not first <= as_of <= last:
        issues.append(
            Issue(
                path="when.as_of",
                message=f"本地{_TARGET_LABELS[target]}数据只覆盖 {first} ~ {last}",
            )
        )
    elif not ds.get_trading_calendar(as_of, as_of):
        issues.append(Issue(path="when.as_of", message=f"{as_of} 不是交易日"))
    return issues + _scope_issues(spec, ds) + _subject_issues(spec, ds)


def _scope_issues(spec: Spec, ds: DataService) -> list[Issue]:
    issues = []
    industry = spec.scope.industry
    if industry is not None:
        kinds = [SW_INDUSTRY]
        if SW_INDUSTRY_L2 in ds.available_targets():
            kinds.append(SW_INDUSTRY_L2)
        names = [board.name for kind in kinds for board in ds.list_boards(kind)]
        if industry not in names:
            issues.append(
                Issue(
                    path="scope.industry",
                    message=f"没有叫「{industry}」的申万行业",
                    allowed="、".join(names),
                )
            )
    board = spec.scope.board
    if board is not None:
        if CONCEPT not in ds.available_targets():
            issues.append(
                Issue(path="scope.board", message=_UNAVAILABLE.format(_TARGET_LABELS[CONCEPT]))
            )
        elif board.code not in {item.code for item in ds.list_boards(CONCEPT)}:
            message = f"没有代码为 {board.code} 的概念板块，可选的见 /api/boards?type=concept"
            issues.append(Issue(path="scope.board.code", message=message))
    return issues


def _subject_issues(spec: Spec, ds: DataService) -> list[Issue]:
    """点名看的标的要在算的范围里，否则排名、对照都算不对。

    范围是全市场时只查代码本身存不存在——展开 5000 多只太贵。
    **只看归属，不看当天被剔除没有**：当天停牌、是 ST、上市不满 60 个交易日的照样出卡，
    卡上写清它没参与排名（research/card.py）。
    """
    if spec.subject.kind == "aggregate":
        return [Issue(path="subject.kind", message="把整个范围算成一个数还不支持")]
    if spec.subject.kind != "codes" or not spec.subject.codes or spec.when.as_of is None:
        return []
    if spec.scope.target != STOCK:
        known = {board.code for board in ds.list_boards(spec.scope.target)}
        missing = [code for code in spec.subject.codes if code not in known]
        message = "没有这个板块代码"
    elif spec.scope.industry or spec.scope.board is not None or spec.scope.base != "all_a":
        inside = set(
            pool_of(spec.scope, spec.when.as_of, ds, exclude=False).get_column("code").to_list()
        )
        outside = [code for code in spec.subject.codes if code not in inside]
        # 当天没有行情的分不出是停牌还是真不属于这个范围，交给卡去说
        traded = _traded(outside, spec.when.as_of, ds)
        missing = [code for code in outside if code in traded]
        message = "不在算的范围里"
    else:
        info = ds.stock_info(list(spec.subject.codes), spec.when.as_of)
        listed = dict(zip(info.get_column("code"), info.get_column("list_date"), strict=True))
        missing = [code for code in spec.subject.codes if listed.get(code) is None]
        message = "本地没有这个股票代码，代码要带交易所后缀，如 600519.SH"
    return [Issue(path="subject.codes", message=f"{code}：{message}") for code in missing]


def _traded(codes: list[str], day: date, ds: DataService) -> set[str]:
    if not codes:
        return set()
    rows = ds.get_fields(codes, day, day, ["close"])
    return set(rows.get_column("code").to_list())


def _event_study_issues(spec: Spec, ds: DataService) -> list[Issue]:
    codes = spec.subject.codes
    if len(codes) != 1:
        return [Issue(path="subject.codes", message="事件统计现在只支持点名一只股票")]
    issues = []
    first, last = ds.data_range(STOCK)
    if ds.stock_info([codes[0]], last).row(0, named=True)["list_date"] is None:
        message = f"本地没有股票代码 {codes[0]}，代码要带交易所后缀，如 600519.SH"
        issues.append(Issue(path="subject.codes", message=message))
    span = spec.when.range
    if span is None:
        return [*issues, Issue(path="when.range", message="事件统计要一段区间")]
    if span.end < first or span.start > last:
        message = f"回看区间 {span.start} ~ {span.end} 不在本地股票数据（{first} ~ {last}）里"
        issues.append(Issue(path="when.range", message=message))
    return issues


# ── 工具 ────────────────────────────────────────────────────────


def issue_text(issue: Issue) -> str:
    """一条问题写成一句话：带上栏目、表达式里的位置、可选范围。"""
    where = f"{issue.path}：" if issue.path else ""
    position = f"第 {issue.position + 1} 个字符，" if issue.position is not None else ""
    allowed = (
        f"（可选：{issue.allowed}）" if issue.allowed and issue.allowed not in issue.message else ""
    )
    return f"{where}{position}{issue.message}{allowed}"


def _under(path: str | None, prefix: str) -> bool:
    return path is not None and (path == prefix or path.startswith(prefix + "."))


def _unique(issues: list[Issue]) -> list[Issue]:
    return list({(issue.path, issue.message): issue for issue in issues}.values())
