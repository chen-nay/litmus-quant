"""QuerySpec：系统的中心数据结构（ARCHITECTURE §4.1）。

它是 llm.plan() 的输出、确认卡的数据源、research.run() 的输入。三种形状靠 shape 区分。

spec 只校验**结构**：栏目齐不齐、类型对不对、数值在不在范围里。表达式写得对不对由 expr.validate() 管，
所以 spec 不依赖 expr。多写了不认识的字段直接报错——LLM 的笔误要在这里拦下，不能悄悄忽略。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from litmus.spec.defaults import (
    BENCHMARKS,
    DEFAULTS,
    MAX_COST_BPS,
    MAX_HORIZON,
    MAX_HORIZONS,
    MAX_LIMIT,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ── 零件 ────────────────────────────────────────────────────────


class BoardRef(_Strict):
    """股票池限定在某个概念板块里。P0 用快照日的当前成分，必须写进 assumptions。"""

    type: Literal["concept"]
    code: str = Field(min_length=1)


class Universe(_Strict):
    base: Literal["all_a", "hs300", "zz500"] = "all_a"
    industry: str | None = None  # 申万一级行业名，按每个交易日当时的归属
    board: BoardRef | None = None
    exclude: tuple[str, ...] = DEFAULTS["exclude"]  # type: ignore[assignment]


class Condition(_Strict):
    expr: str = Field(min_length=1)
    label: str = ""


class Sort(_Strict):
    by: str = Field(min_length=1)
    order: Literal["asc", "desc"] = "desc"


class Target(_Strict):
    mention: str = ""  # 用户原话里的提及
    guess: str | None = None  # LLM 猜的全称
    code: str | None = None  # 由 api 调 ds.resolve_stock() 填写，LLM 不填


class Event(_Strict):
    preset_id: str | None = None  # 事件库里的编号（第 4 步）
    params: dict[str, int | float] = Field(default_factory=dict)
    expr: str = Field(min_length=1)  # 由代码从模板渲染，LLM 不写
    label: str = ""
    library_version: int | None = None  # 生成表达式时的事件库版本；模板改过之后重跑旧记录能发现


class TimeRange(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    start: date = Field(alias="from")
    end: date = Field(alias="to")

    @model_validator(mode="after")
    def _ordered(self) -> TimeRange:
        if self.start > self.end:
            raise ValueError(f"回看区间的起点 {self.start} 晚于终点 {self.end}")
        return self


# ── 三种形状 ────────────────────────────────────────────────────


class _Common(_Strict):
    version: Literal[1] = 1
    defaults_used: tuple[str, ...] = ()  # 哪些字段用的是默认值，确认卡据此标出「默认值，可修改」
    assumptions: tuple[str, ...] = ()  # 由代码从 spec 生成（第 7 步），LLM 不写


class StockListSpec(_Common):
    """股票表：某一天的股票池里筛选 → 排序 → 取前 N。筛选、排序都可以为空。"""

    shape: Literal["stock_list"]
    as_of: date
    filter: Condition | None = None
    universe: Universe = Universe()
    sort: Sort | None = None
    limit: int = Field(DEFAULTS["top_n"], ge=1, le=MAX_LIMIT)


class BoardListSpec(_Common):
    """板块表：某一天某个口径的全部板块里筛选 → 排序 → 取前 N。"""

    shape: Literal["board_list"]
    board_type: Literal["sw_industry", "concept"]
    as_of: date
    filter: Condition | None = None
    sort: Sort | None = None
    limit: int = Field(DEFAULTS["top_n"], ge=1, le=MAX_LIMIT)


class StockHistorySpec(_Common):
    """个股回看：找出事件的每一次触发，算之后 N 天涨跌，和同期市场、这只股票平时比。"""

    shape: Literal["stock_history"]
    target: Target
    event: Event
    time_range: TimeRange
    horizons: tuple[int, ...] = DEFAULTS["horizons"]  # type: ignore[assignment]
    benchmark: Literal[BENCHMARKS] = DEFAULTS["benchmark"]  # type: ignore[valid-type]
    cost_bps: float = Field(DEFAULTS["cost_bps"], ge=0, le=MAX_COST_BPS)

    @field_validator("horizons")
    @classmethod
    def _horizons(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        """去重、升序；每档 1 ~ MAX_HORIZON 个交易日。"""
        if not value:
            raise ValueError("至少要看一档持有天数")
        if any(not 1 <= n <= MAX_HORIZON for n in value):
            raise ValueError(f"持有天数要在 1 ~ {MAX_HORIZON} 个交易日之间")
        normalized = tuple(sorted(set(value)))
        if len(normalized) > MAX_HORIZONS:
            raise ValueError(f"持有天数最多 {MAX_HORIZONS} 档")
        return normalized


QuerySpec = Annotated[
    StockListSpec | BoardListSpec | StockHistorySpec, Field(discriminator="shape")
]

_ADAPTER: TypeAdapter[StockListSpec | BoardListSpec | StockHistorySpec] = TypeAdapter(QuerySpec)


def parse_spec(data: object) -> StockListSpec | BoardListSpec | StockHistorySpec:
    """dict / JSON 解析出来的对象 → QuerySpec。不合格抛 pydantic.ValidationError。"""
    return _ADAPTER.validate_python(data)
