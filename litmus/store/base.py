"""研究记录的数据结构与存储接口（ARCHITECTURE §7）。

- 接口用业务语言（存一条计划、取一条运行记录），不出现读文件、列目录这类操作
- **store 不认识上层模块的类型**：研究请求和结果都是已经转好的 dict，转换由 api 负责。
  store 和 research 同层，直接引用 ListResult / HistoryResult 会违反依赖规则
- 以后换 SQLite：新增一个实现同一接口的类，通过同一套契约测试，api 换个类名即可
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PlanRecord:
    """一次提问：用户原话和 llm.plan() 生成的研究请求（第 7 步用）。"""

    query: str
    status: str  # ok / needs_clarification / unsupported / not_an_event / failed
    spec: dict[str, object] | None = None
    detail: dict[str, object] = field(default_factory=dict)  # 澄清问题、改写建议等
    plan_id: str = ""  # 保存时由 store 填
    created_at: str = ""


@dataclass(frozen=True)
class RunRecord:
    """一次研究计算：研究请求、结果，以及当时的数据状态——数据同步过、事件库改过之后，重算的结果会不一样。"""

    spec: dict[str, object]
    status: str  # done / failed
    result: dict[str, object] | None = None
    error: str | None = None  # failed 时的错误摘要
    plan_id: str | None = None
    data_through: str | None = None  # 当时本地数据截至哪天
    library_version: int | None = None  # 个股回看生成事件表达式时的事件库版本
    duration_ms: int | None = None
    run_id: str = ""  # 保存时由 store 填
    created_at: str = ""


@dataclass(frozen=True)
class TraceRecord:
    """一次提问 / 一次运行的过程：每一步调了什么、返回了什么、花了多久（ARCHITECTURE §7）。

    **不另编号**：编号就是它记录的那条 plan_id / run_id，文件名即关联。
    steps 是一串 dict，形状由写入方决定，store 不认识里面的内容。
    """

    record_id: str  # plan_id 或 run_id
    query: str = ""
    steps: list[dict[str, object]] = field(default_factory=list)
    created_at: str = ""  # 保存时由 store 填


@dataclass(frozen=True)
class NarrativeRecord:
    """卡下面的小结：大模型拿卡上的内容写的一段话（DESIGN.md §1.6）。

    卡先出、小结后到，所以不写进运行记录，单独存一条。**不另编号**：编号就是那次运行的 run_id。
    calls 是这次写小结的每次大模型调用（重试就有两条），形状由写入方定。
    """

    run_id: str
    text: str  # 空串：没什么可说，或者两次都没写对
    error: str | None = None  # 没写出来的原因
    calls: list[dict[str, object]] = field(default_factory=list)
    created_at: str = ""  # 保存时由 store 填


class Store(Protocol):
    def save_plan(self, plan: PlanRecord) -> str:
        """保存一条提问记录，返回 plan_id。"""

    def get_plan(self, plan_id: str) -> PlanRecord | None:
        """取提问记录；不存在或编号不合法返回 None。"""

    def save_run(self, run: RunRecord) -> str:
        """保存一条运行记录，返回 run_id。"""

    def get_run(self, run_id: str) -> RunRecord | None:
        """取运行记录；不存在或编号不合法返回 None。"""

    def save_trace(self, trace: TraceRecord) -> str:
        """保存一条过程记录，编号用 trace.record_id（plan_id / run_id），返回它。"""

    def get_trace(self, record_id: str) -> TraceRecord | None:
        """取过程记录；不存在或编号不合法返回 None。"""

    def save_narrative(self, narrative: NarrativeRecord) -> str:
        """保存一次运行的小结，编号用 narrative.run_id，返回它。"""

    def get_narrative(self, run_id: str) -> NarrativeRecord | None:
        """取小结；还没写、或编号不合法返回 None。"""
