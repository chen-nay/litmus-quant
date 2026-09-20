"""QuerySpec v2：系统的中心数据结构（DESIGN.md §1）。

它是 `llm.plan()` 的输出、确认卡的数据源、`research.run()` 的输入。五个正交维度：

    scope    在谁身上算   —— 也是 Rank 的范围和「全A等权」对照的口径
    subject  最后看谁     —— pool 整个池子 / codes 点名 / aggregate 聚合成一个数
    when     哪天、哪段
    metrics  算哪些数     —— 一项 = 一个名字 + 一个公式
    output   怎么出       —— table 筛排取前N / card 直接显示 / event_study 事件统计

**算和用是两件事**：`metrics` 只负责把数算出来，算出来的那一列就叫它的 `name`；
拿这些数干什么由 `output` 决定——`sort.by` 填的是某个 metric 的 `name`，不是公式。
`filter` 仍然写表达式，它是个条件（真假），不是要显示的数。

spec 只校验**结构**：栏目齐不齐、类型对不对、数值在不在范围里、组合合不合法。
表达式写得对不对由 `expr.validate()` 管，代码和行业名存不存在由 `api/checks.py` 查，
所以 spec 不依赖 expr、也不依赖 data。多写了不认识的字段直接报错——
大模型的笔误要在这里拦下，不能悄悄忽略。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from litmus.spec.defaults import (
    BENCHMARKS,
    CARD_BENCHMARKS,
    DEFAULTS,
    MAX_COST_BPS,
    MAX_HORIZON,
    MAX_HORIZONS,
    MAX_LIMIT,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ── 严格类型 ────────────────────────────────────────────────────
# pydantic 默认会顺手转换：整数 1757548800 当成时间戳变成 2025-09-11，true 当成 1，"5" 当成 5，NaN 照收。
# 请求来自前端和大模型，这种转换只会把写错的值悄悄变成另一个意思，所以只收明确的写法

_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _day(value: object) -> object:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and _ISO_DAY.fullmatch(value):
        return value
    raise ValueError(f"日期要写成 YYYY-MM-DD，收到 {value!r}")


def _numeric(kind: str) -> Callable[[object], object]:
    def check(value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"要是{kind}，收到 {value!r}")
        return value

    return check


#: 日期：只收 "YYYY-MM-DD"
Day = Annotated[date, BeforeValidator(_day)]
#: 整数：60.0 这种没有小数部分的也收（和事件参数一致）；60.5、布尔值、字符串不收
WholeNumber = Annotated[int, BeforeValidator(_numeric("整数"))]
#: 数字：布尔值、字符串不收；NaN、无穷大由字段上的 allow_inf_nan=False 拦
Number = Annotated[float, BeforeValidator(_numeric("数字"))]

#: 标的类型
TARGETS = ("stock", "sw_industry", "sw_industry_l2", "concept")


# ── scope：在谁身上算 ───────────────────────────────────────────


class BoardRef(_Strict):
    """限定在某个概念板块里。P0 用快照日的当前成分，必须写进 assumptions。"""

    type: Literal["concept"]
    code: str = Field(min_length=1)


class Scope(_Strict):
    """算的范围。决定哪些标的参与计算，也是 Rank 的排名范围。

    和 subject 是两件事：「牧原在农林牧渔里涨幅排第几」——scope 是农林牧渔全部，
    排名在这里面算；subject 是牧原一只，最后只显示它。
    """

    target: Literal[TARGETS] = "stock"  # type: ignore[valid-type]
    base: Literal["all_a", "hs300", "zz500"] = "all_a"  # 只对 target=stock 有意义
    industry: str | None = None  # 申万一级或二级行业名，按每个交易日当时的归属
    board: BoardRef | None = None
    exclude: tuple[str, ...] = DEFAULTS["exclude"]  # type: ignore[assignment]


# ── subject：最后看谁 ───────────────────────────────────────────


class Mention(_Strict):
    """用户原话里说的标的。代码由 api 调 ds.resolve_* 查，大模型不填代码。"""

    mention: str = Field(min_length=1)
    guess: str | None = None  # 大模型猜的**全称**，不是代码


class Subject(_Strict):
    """看的对象。

    - pool：整个 scope，配 output=table
    - codes：点名看这几个，配 output=card / event_study。行由用户点名，这是卡和表的区别
    - aggregate：对整个 scope 求一个数，没有标的
    """

    kind: Literal["pool", "codes", "aggregate"] = "pool"
    #: kind=codes 时用户原话里说的标的，大模型填
    mentions: tuple[Mention, ...] = ()
    #: kind=codes 时解析出来的代码，由 api 填
    codes: tuple[str, ...] = ()


# ── when：哪天、哪段 ────────────────────────────────────────────


class DateRange(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    start: Day = Field(alias="from")
    end: Day = Field(alias="to")

    @model_validator(mode="after")
    def _ordered(self) -> DateRange:
        if self.start > self.end:
            raise ValueError(f"区间的起点 {self.start} 晚于终点 {self.end}")
        return self


class When(_Strict):
    """时点或区间，二选一。没填由 api 按这个查询实际用到哪类数据回填。"""

    as_of: Day | None = None
    range: DateRange | None = None

    @model_validator(mode="after")
    def _one_of(self) -> When:
        if self.as_of is not None and self.range is not None:
            raise ValueError("as_of 和 range 只能填一个：看某一天用 as_of，看一段用 range")
        return self


# ── metrics：算哪些数 ───────────────────────────────────────────


class Metric(_Strict):
    """一个名字 + 一个公式。含义只有一个：把这个数算出来，那一列就叫这个名字。

    `name` 也是别处引用它的钥匙——`output.sort.by` 填的就是这里的 name。
    """

    name: str = Field(min_length=1)
    expr: str = Field(min_length=1)


# ── output：怎么出 ──────────────────────────────────────────────


class Condition(_Strict):
    """筛选条件。写表达式：它是个真假，不是要显示的数。"""

    expr: str = Field(min_length=1)
    label: str = ""


class Sort(_Strict):
    by: str = Field(min_length=1)  # 某个 metric 的 name
    order: Literal["asc", "desc"] = "desc"


class TableOutput(_Strict):
    """筛 → 排 → 取前 N，metrics 全部显示。"""

    kind: Literal["table"]
    filter: Condition | None = None
    sort: Sort | None = None
    limit: WholeNumber = Field(DEFAULTS["top_n"], ge=1, le=MAX_LIMIT)  # type: ignore[arg-type]


class CardOutput(_Strict):
    """不筛不排，metrics 直接显示。没有 filter / sort / limit——多填的字段由 extra=forbid 拦掉。

    唯一能选的是涨跌和谁比：默认所属申万一级行业指数，问「跑赢沪深300了吗」时换成指数。
    """

    kind: Literal["card"]
    benchmark: Literal[CARD_BENCHMARKS] = "industry"  # type: ignore[valid-type]


class Event(_Strict):
    preset_id: str | None = None  # 事件库里的编号
    params: dict[str, int | float] = Field(default_factory=dict)
    expr: str = Field(min_length=1)  # 由代码从模板渲染，大模型不写
    label: str = ""
    library_version: int | None = None  # 生成表达式时的事件库版本


class EventStudyOutput(_Strict):
    """找出事件的每一次触发，算之后 N 天涨跌，和同期市场比。不看 metrics。"""

    kind: Literal["event_study"]
    event: Event
    horizons: tuple[WholeNumber, ...] = DEFAULTS["horizons"]  # type: ignore[assignment]
    benchmark: Literal[BENCHMARKS] = DEFAULTS["benchmark"]  # type: ignore[valid-type]
    cost_bps: Number = Field(DEFAULTS["cost_bps"], ge=0, le=MAX_COST_BPS, allow_inf_nan=False)  # type: ignore[arg-type]

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


Output = Annotated[TableOutput | CardOutput | EventStudyOutput, Field(discriminator="kind")]


# ── 合起来 ──────────────────────────────────────────────────────


class QuerySpec(_Strict):
    version: Literal[2] = 2
    scope: Scope = Scope()
    subject: Subject = Subject()
    when: When = When()
    metrics: tuple[Metric, ...] = ()
    output: Output
    #: 卡下面再写一段话。只在 output=card 时可用
    narrate: bool = False
    #: 哪些字段用的是默认值，确认卡据此标出「默认值，可修改」。由 api 填
    defaults_used: tuple[str, ...] = ()
    #: 确认卡上的说明，由代码从 spec 生成。大模型和前端填的都不作数
    assumptions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _combinations(self) -> QuerySpec:
        """维度之间的非法组合（DESIGN.md §3）。只看结构，要查数据的在 api/checks.py。"""
        for check in (
            self._metric_names,
            self._subject_and_output,
            self._when_and_output,
            self._metrics_and_output,
            self._sort_refers_to_metric,
            self._narrate,
        ):
            check()
        return self

    # ①⑩ metrics 自身
    def _metric_names(self) -> None:
        names = [metric.name for metric in self.metrics]
        dupes = sorted({name for name in names if names.count(name) > 1})
        if dupes:
            raise ValueError(f"指标重名了：{'、'.join(dupes)}。名字要能唯一指到一列")

    # ①⑥⑦ subject 和 output
    def _subject_and_output(self) -> None:
        kind = self.output.kind
        if self.subject.kind == "pool" and kind == "card":
            raise ValueError("卡要点名看谁（subject.kind=codes），或者把 output 改成 table")
        if self.subject.kind == "aggregate" and kind != "card":
            raise ValueError("聚合只有一个数，没有行也没有触发点，output 只能是 card")
        if self.subject.kind == "codes" and not (self.subject.codes or self.subject.mentions):
            raise ValueError("subject.kind=codes 要给出看的是谁（mentions 或 codes）")

    # ④⑤ when 和 output
    def _when_and_output(self) -> None:
        if self.output.kind == "event_study":
            if self.when.as_of is not None:
                raise ValueError("事件统计要一段区间（when.range），不是某一天")
        elif self.when.range is not None:
            raise ValueError(f"{self.output.kind} 看的是某一天（when.as_of），不是一段区间")

    # ②③ metrics 和 output
    def _metrics_and_output(self) -> None:
        if self.output.kind == "card" and not self.metrics:
            raise ValueError("卡上要有指标：metrics 至少给一项")
        if self.output.kind == "event_study" and self.metrics:
            raise ValueError("事件统计不看展示指标，metrics 不要填")

    # ⑨ sort.by 要指向一个 metric
    def _sort_refers_to_metric(self) -> None:
        sort = getattr(self.output, "sort", None)
        if sort is None:
            return
        names = [metric.name for metric in self.metrics]
        if sort.by not in names:
            available = "、".join(names) if names else "（还没有指标）"
            raise ValueError(f"sort.by 要填一个指标的名字，收到「{sort.by}」，可选：{available}")

    # ⑧ narrate
    def _narrate(self) -> None:
        if self.narrate and self.output.kind != "card":
            raise ValueError("总结只跟着卡走，output 不是 card 时 narrate 要留空")


_ADAPTER: TypeAdapter[QuerySpec] = TypeAdapter(QuerySpec)


def parse_spec(data: object) -> QuerySpec:
    """dict / JSON 解析出来的对象 → QuerySpec。不合格抛 pydantic.ValidationError。"""
    return _ADAPTER.validate_python(data)
