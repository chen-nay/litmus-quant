"""/api/run 的确定性检查（ARCHITECTURE §1.4 ②）：任何一条不过就返回 needs_revision，绝不执行。

按顺序查，前一类有问题就不往下查（结构都不对，谈不上查表达式和数据）：

1. **事件**：个股回看只能用事件库里的事件。按「编号 + 参数」重新生成表达式和标签，请求里带来的表达式、标签不作数——
   用户在确认卡上改了参数也走这一步，不再调 LLM
2. **结构**：栏目齐不齐、类型对不对、数值在不在范围里（spec.parse_spec），说明翻成中文
3. **表达式**：写法和字段、算子白名单（expr.parse + expr.validate），标的类型当前可用
4. **数据**：日期是交易日、落在本地数据范围里；股票代码、申万行业名、概念板块代码本地查得到
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from litmus.api.models import Issue
from litmus.data import CONCEPT, STOCK, SW_INDUSTRY, DataService
from litmus.expr import ExprSyntaxError, parse, validate
from litmus.signals import EventLibrary, EventParamError, render_event
from litmus.spec import BoardListSpec, StockHistorySpec, StockListSpec, parse_spec

Spec = StockListSpec | BoardListSpec | StockHistorySpec

_SHAPES = ("stock_list", "board_list", "stock_history")
_TARGET_LABELS = {STOCK: "股票", SW_INDUSTRY: "申万行业", CONCEPT: "概念板块"}
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
    "union_tag_invalid": "shape 只能是 {expected_tags}",
    "union_tag_not_found": "缺少 shape，可选 'stock_list'、'board_list'、'stock_history'",
    "finite_number": "要是有限的数字",
}
_PREFIXES = (("date", "日期要写成 YYYY-MM-DD"), ("int", "要是整数"), ("float", "要是数字"))


def check_spec(
    raw: object, ds: DataService, events: EventLibrary
) -> tuple[Spec | None, list[Issue]]:
    """通过返回 (spec, [])，个股回看的事件已经按事件库重新生成；不通过返回 (None, 要改的地方)。"""
    if not isinstance(raw, dict):
        return None, [Issue(path="spec", message="spec 要是一个对象")]
    raw, event_issues = _render_event(raw, events)
    try:
        spec = parse_spec(raw)
    except ValidationError as exc:
        structure = _structure_issues(exc)
        if event_issues:  # 事件已经报过了，别再连带报「event.expr 缺少这一项」
            structure = [issue for issue in structure if not _under(issue.path, "event")]
        return None, _unique(event_issues + structure)
    if event_issues:
        return None, event_issues
    issues = _expression_issues(spec, ds) or _data_issues(spec, ds)
    return (None, issues) if issues else (spec, [])


# ── 1. 事件 ─────────────────────────────────────────────────────


def _render_event(raw: dict[str, Any], events: EventLibrary) -> tuple[dict[str, Any], list[Issue]]:
    if raw.get("shape") != "stock_history" or not isinstance(raw.get("event"), dict):
        return raw, []  # 不是个股回看，或者事件栏目本身就不对（结构检查会报）
    event = raw["event"]
    preset_id = event.get("preset_id")
    if not preset_id:
        message = "个股回看的事件要从事件库里选，可选的见 /api/events（暂不支持自定义事件）"
        return raw, [Issue(path="event.preset_id", message=message)]
    params = event.get("params") or {}
    if not isinstance(params, Mapping):
        return raw, []  # 结构检查会报 event.params
    try:
        rendered = render_event(str(preset_id), params, events)
    except EventParamError as exc:
        path = "event.preset_id" if exc.param is None else f"event.params.{exc.param}"
        return raw, [Issue(path=path, message=str(exc), allowed=exc.allowed)]
    event = {
        **event,
        "preset_id": rendered.preset_id,
        "params": rendered.params,
        "expr": rendered.expr,
        "label": rendered.label,
        "library_version": rendered.library_version,
    }
    return {**raw, "event": event}, []


# ── 2. 结构 ─────────────────────────────────────────────────────


def _structure_issues(exc: ValidationError) -> list[Issue]:
    issues = []
    for error in exc.errors():
        loc = [str(part) for part in error["loc"]]
        if loc and loc[0] in _SHAPES:  # 按 shape 分派时，pydantic 把形状名放在位置的最前面
            loc = loc[1:]
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


# ── 3. 表达式 ───────────────────────────────────────────────────


def _expression_issues(spec: Spec, ds: DataService) -> list[Issue]:
    if isinstance(spec, StockHistorySpec):
        return []  # 事件表达式来自事件库，库加载时已经把全部参数组合校验过
    target = STOCK if isinstance(spec, StockListSpec) else spec.board_type
    if target not in ds.available_targets():
        return [Issue(path="board_type", message=_UNAVAILABLE.format(_TARGET_LABELS[target]))]
    issues: list[Issue] = []
    if spec.filter is not None:
        issues += _expr_issues("filter.expr", spec.filter.expr, target, "filter")
    if spec.sort is not None:
        issues += _expr_issues("sort.by", spec.sort.by, target, "sort")
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


# ── 4. 数据 ─────────────────────────────────────────────────────


def _data_issues(spec: Spec, ds: DataService) -> list[Issue]:
    if isinstance(spec, StockHistorySpec):
        return _history_issues(spec, ds)
    target = STOCK if isinstance(spec, StockListSpec) else spec.board_type
    issues = []
    first, last = ds.data_range(target)
    if not first <= spec.as_of <= last:
        message = f"本地{_TARGET_LABELS[target]}数据只覆盖 {first} ~ {last}"
        issues.append(Issue(path="as_of", message=message))
    elif not ds.get_trading_calendar(spec.as_of, spec.as_of):
        issues.append(Issue(path="as_of", message=f"{spec.as_of} 不是交易日"))
    if isinstance(spec, StockListSpec):
        issues += _universe_issues(spec, ds)
    return issues


def _universe_issues(spec: StockListSpec, ds: DataService) -> list[Issue]:
    issues = []
    industry = spec.universe.industry
    if industry is not None:
        names = [board.name for board in ds.list_boards(SW_INDUSTRY)]
        if industry not in names:
            issues.append(
                Issue(
                    path="universe.industry",
                    message=f"没有叫「{industry}」的申万一级行业",
                    allowed="、".join(names),
                )
            )
    board = spec.universe.board
    if board is not None:
        if CONCEPT not in ds.available_targets():
            message = _UNAVAILABLE.format(_TARGET_LABELS[CONCEPT])
            issues.append(Issue(path="universe.board", message=message))
        elif board.code not in {item.code for item in ds.list_boards(CONCEPT)}:
            message = f"没有代码为 {board.code} 的概念板块，可选的见 /api/boards?type=concept"
            issues.append(Issue(path="universe.board.code", message=message))
    return issues


def _history_issues(spec: StockHistorySpec, ds: DataService) -> list[Issue]:
    code = spec.target.code
    if not code:
        return [Issue(path="target.code", message="要填股票代码，如 600519.SH")]
    issues = []
    first, last = ds.data_range(STOCK)
    if ds.stock_info([code], last).row(0, named=True)["list_date"] is None:
        message = f"本地没有股票代码 {code}，代码要带交易所后缀，如 600519.SH"
        issues.append(Issue(path="target.code", message=message))
    window = spec.time_range
    if window.end < first or window.start > last:
        message = f"回看区间 {window.start} ~ {window.end} 不在本地股票数据（{first} ~ {last}）里"
        issues.append(Issue(path="time_range", message=message))
    return issues


# ── 工具 ────────────────────────────────────────────────────────


def _under(path: str | None, prefix: str) -> bool:
    return path is not None and (path == prefix or path.startswith(prefix + "."))


def _unique(issues: list[Issue]) -> list[Issue]:
    return list({(issue.path, issue.message): issue for issue in issues}.values())
