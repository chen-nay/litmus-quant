"""事件库：加载 builtin.toml、校验、按「编号 + 参数」生成表达式（ARCHITECTURE §4.3）。

- **加载时就查出所有写错的地方**：文件结构、模板占位符和参数对不上、默认值不在范围里、示例参数不合法；
  每个事件的所有合法参数组合（可选值全部、数值参数取最小 / 默认 / 最大）都生成一遍、过表达式校验，
  预热条数超限也在这里拦下。写错了应用启动就报错，不会等到用户问到那个参数才发现
- **参数越界不自动改**：报错说明可选范围，由上层让用户改参数（2026-09-14 定）。报错带上参数名、传入的值、
  可选范围，上层不用解析文字
- 整数参数遇到 60.0 当成 60，60.5 报错；布尔值不收——JSON 里的 true 在 Python 里也是整数
"""

from __future__ import annotations

import functools
import itertools
import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from litmus.data import STOCK
from litmus.expr import ExprSyntaxError, onset, parse, validate

Number = int | float

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class EventDefinitionError(ValueError):
    """事件库文件本身写错了。加载时就报，不带到运行时。"""


class EventParamError(ValueError):
    """请求里的事件编号或参数不合法。上层据此让用户改参数。"""

    def __init__(
        self,
        message: str,
        *,
        preset_id: str,
        param: str | None = None,
        value: object = None,
        allowed: str | None = None,
    ):
        super().__init__(message)
        self.preset_id = preset_id
        self.param = param
        self.value = value
        self.allowed = allowed


# ── 文件结构 ────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ParamSpec(_Strict):
    label: str
    unit: str = ""
    kind: Literal["choice", "int", "float"]
    default: Number
    choices: tuple[int, ...] = ()
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ParamSpec:
        if self.kind == "choice":
            if not self.choices or self.min is not None or self.max is not None:
                raise ValueError("choice 参数要给 choices，不给 min、max")
            if list(self.choices) != sorted(set(self.choices)):
                raise ValueError("choices 要从小到大、不重复")
            if self.default not in self.choices:
                raise ValueError(f"默认值 {self.default} 不在 choices 里")
            return self
        if self.choices or self.min is None or self.max is None or self.min >= self.max:
            raise ValueError(f"{self.kind} 参数要给 min < max，不给 choices")
        if not self.min <= self.default <= self.max:
            raise ValueError(f"默认值 {self.default} 不在 {_show(self.min)}~{_show(self.max)} 里")
        if self.kind == "int" and not all(
            float(v).is_integer() for v in (self.min, self.max, self.default)
        ):
            raise ValueError("int 参数的 min、max、默认值都要是整数")
        return self

    @property
    def allowed(self) -> str:
        if self.kind == "choice":
            return "/".join(str(choice) for choice in self.choices)
        return f"{_show(self.min)}~{_show(self.max)}"

    def default_value(self) -> Number:
        return float(self.default) if self.kind == "float" else int(self.default)

    def samples(self) -> list[Number]:
        """加载时校验用：可选值全部；数值参数取最小、默认、最大。"""
        if self.kind == "choice":
            return list(self.choices)
        values = sorted({float(self.min), float(self.default), float(self.max)})  # type: ignore[arg-type]
        return [int(v) for v in values] if self.kind == "int" else values


class Example(_Strict):
    question: str = Field(min_length=1)
    params: dict[str, Number] = Field(default_factory=dict)


class EventDefinition(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    template: str = Field(min_length=1)
    label: str = Field(min_length=1)
    example: Example
    less_than: tuple[tuple[str, str], ...] = ()
    params: dict[str, ParamSpec] = Field(default_factory=dict)


class EventLibrary(_Strict):
    version: int = Field(ge=1)
    events: tuple[EventDefinition, ...]

    def find(self, preset_id: str) -> EventDefinition:
        for event in self.events:
            if event.id == preset_id:
                return event
        choices = "、".join(f"{event.id}（{event.name}）" for event in self.events)
        raise EventParamError(f"没有事件 {preset_id!r}，可选：{choices}", preset_id=preset_id)


# ── 加载 ────────────────────────────────────────────────────────


@functools.cache
def load_events() -> EventLibrary:
    """加载并校验 litmus/signals/builtin.toml。结果缓存，应用启动时调一次就能发现写错的地方。"""
    text = resources.files("litmus.signals").joinpath("builtin.toml").read_text(encoding="utf-8")
    return parse_library(tomllib.loads(text))


def parse_library(raw: Mapping[str, object]) -> EventLibrary:
    """解析并校验事件库的原始内容（TOML 解析出来的 dict）。"""
    try:
        library = EventLibrary.model_validate(raw)
    except ValidationError as exc:
        raise EventDefinitionError(f"事件库文件结构不对：{exc}") from exc
    _check(library)
    return library


def _check(library: EventLibrary) -> None:
    ids = [event.id for event in library.events]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise EventDefinitionError(f"事件编号重复：{duplicated}")
    for event in library.events:
        names = set(event.params)
        used = set(_PLACEHOLDER.findall(event.template))
        if used != names:
            raise EventDefinitionError(
                f"{event.id}：模板里的占位符 {sorted(used)} 和参数 {sorted(names)} 对不上"
            )
        stray = set(_PLACEHOLDER.findall(event.label)) - names
        if stray:
            raise EventDefinitionError(f"{event.id}：标签里有不存在的参数 {sorted(stray)}")
        for smaller, larger in event.less_than:
            if smaller not in names or larger not in names:
                raise EventDefinitionError(
                    f"{event.id}：约束 {smaller} < {larger} 用了不存在的参数"
                )
        try:
            _resolve(event, event.example.params)
        except EventParamError as exc:
            raise EventDefinitionError(f"{event.id}：示例参数不合法：{exc}") from exc
        _check_all_combinations(event)


def _check_all_combinations(event: EventDefinition) -> None:
    names = list(event.params)
    for combination in itertools.product(*(event.params[name].samples() for name in names)):
        values = dict(zip(names, combination, strict=True))
        if any(values[smaller] >= values[larger] for smaller, larger in event.less_than):
            continue
        text = _fill(event.template, values, _expr_number)
        try:
            node = parse(text)
        except ExprSyntaxError as exc:
            raise EventDefinitionError(f"{event.id} 取 {values} 时模板写法不对：{exc}") from exc
        result = validate(onset(node), STOCK, "event")
        if not result.ok:
            issues = "；".join(str(issue) for issue in result.issues)
            raise EventDefinitionError(f"{event.id} 取 {values} 时表达式通不过校验：{issues}")


# ── 生成表达式 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class RenderedEvent:
    preset_id: str
    library_version: int
    params: dict[str, Number]  # 补全默认值之后的全部参数
    defaults_used: tuple[str, ...]  # 用了默认值的参数名
    expr: str  # 条件本身；「由不满足变为满足」由研究计算统一包
    label: str


def render_event(
    preset_id: str, params: Mapping[str, object] | None = None, library: EventLibrary | None = None
) -> RenderedEvent:
    """按「编号 + 参数」生成事件表达式和标签。编号不存在、参数不认识或越界，抛 EventParamError。"""
    library = library or load_events()
    event = library.find(preset_id)
    values, defaults_used = _resolve(event, params or {})
    return RenderedEvent(
        preset_id=event.id,
        library_version=library.version,
        params=values,
        defaults_used=defaults_used,
        expr=_fill(event.template, values, _expr_number),
        label=_fill(event.label, values, _show),
    )


def event_catalog(library: EventLibrary | None = None) -> list[dict[str, object]]:
    """事件清单：给 LLM 提示词和 /api/events 用，和生成表达式用的是同一份定义。"""
    library = library or load_events()
    return [
        {
            "id": event.id,
            "name": event.name,
            "category": event.category,
            "template": event.template,
            "label": event.label,
            "params": [
                {
                    "name": name,
                    "label": spec.label,
                    "unit": spec.unit,
                    "allowed": spec.allowed,
                    "default": spec.default_value(),
                }
                for name, spec in event.params.items()
            ],
            "constraints": [
                f"{event.params[smaller].label}要小于{event.params[larger].label}"
                for smaller, larger in event.less_than
            ],
            "example": {"question": event.example.question, "params": dict(event.example.params)},
        }
        for event in library.events
    ]


def _resolve(
    event: EventDefinition, given: Mapping[str, object]
) -> tuple[dict[str, Number], tuple[str, ...]]:
    unknown = [name for name in given if name not in event.params]
    if unknown:
        available = (
            "、".join(f"{name}（{spec.label}）" for name, spec in event.params.items()) or "无"
        )
        raise EventParamError(
            f"「{event.name}」没有参数 {unknown[0]}，可调的参数：{available}",
            preset_id=event.id,
            param=unknown[0],
            value=given[unknown[0]],
            allowed=available,
        )
    values: dict[str, Number] = {}
    defaults_used: list[str] = []
    for name, spec in event.params.items():
        if name in given:
            values[name] = _coerce(event, name, spec, given[name])
        else:
            values[name] = spec.default_value()
            defaults_used.append(name)
    for smaller, larger in event.less_than:
        if values[smaller] >= values[larger]:
            small_spec, large_spec = event.params[smaller], event.params[larger]
            raise EventParamError(
                f"「{event.name}」的{small_spec.label}（{smaller}={_show(values[smaller])}）"
                f"要小于{large_spec.label}（{larger}={_show(values[larger])}）",
                preset_id=event.id,
                param=smaller,
                value=values[smaller],
                allowed=f"小于{large_spec.label}",
            )
    return values, tuple(defaults_used)


def _coerce(event: EventDefinition, name: str, spec: ParamSpec, value: object) -> Number:
    def fail(reason: str) -> NoReturn:
        raise EventParamError(
            f"「{event.name}」的{spec.label}（{name}）{reason}",
            preset_id=event.id,
            param=name,
            value=value,
            allowed=spec.allowed,
        )

    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        fail(f"要是数字，可选 {spec.allowed}，收到 {value!r}")
    if spec.kind == "float":
        if not spec.min <= value <= spec.max:  # type: ignore[operator]
            fail(f"可选 {spec.allowed}，收到 {_show(value)}")
        return float(value)
    if isinstance(value, float) and not value.is_integer():
        fail(f"要是整数，可选 {spec.allowed}，收到 {_show(value)}")
    number = int(value)
    if spec.kind == "choice" and number not in spec.choices:
        fail(f"可选 {spec.allowed}，收到 {number}")
    if spec.kind == "int" and not spec.min <= number <= spec.max:  # type: ignore[operator]
        fail(f"可选 {spec.allowed}，收到 {number}")
    return number


def _fill(template: str, values: Mapping[str, Number], show) -> str:
    return _PLACEHOLDER.sub(lambda match: show(values[match.group(1)]), template)


def _expr_number(value: Number) -> str:
    """代入表达式：整数写整数（窗口参数必须是整数字面量），小数保留小数点。"""
    return str(value) if isinstance(value, int) else repr(float(value))


def _show(value: Number | None) -> str:
    """给人看：3.0 显示成 3，2.5 显示成 2.5。"""
    return f"{value:g}" if isinstance(value, float) else str(value)
