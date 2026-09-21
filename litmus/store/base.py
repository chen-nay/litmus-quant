"""研究记录的数据结构与存储接口（ARCHITECTURE §7）。

- 接口用业务语言（存一条提问、取一条运行记录），不出现读文件、列目录这类操作
- **store 不认识上层模块的类型**：研究请求和结果都是已经转好的 dict，转换由 api 负责。
  store 和 research 同层，直接引用 ListResult / HistoryResult 会违反依赖规则
- **一件事一个文件**：提问的过程、运行的过程、卡下面的小结，都写在它们各自的记录里，
  查一次提问只看一个文件
- 以后换 SQLite：新增一个实现同一接口的类，通过同一套契约测试，api 换个类名即可
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Narrative:
    """卡下面的小结：大模型拿卡上的内容写的一段话（DESIGN.md §1.6）。

    卡先出、小结后到，所以是算完之后补写进运行记录的。写它的每次大模型调用记在
    同一条运行记录的 steps 里。
    """

    text: str  # 空串：没什么可说，或者两次都没写对
    error: str | None = None  # 没写出来的原因
    created_at: str = ""  # 保存时由 store 填


@dataclass(frozen=True)
class PlanRecord:
    """一次提问：用户原话、翻出来的研究请求，以及这中间每一步都干了什么。

    steps 是一串 dict，形状由写入方决定（大模型调了什么、查名字、检查、检查接口），store 不认识里面的内容。
    runs 是这次提问引出的运行编号，一次提问可以跑出多次（改完条件重跑）。
    """

    query: str
    status: str  # ok / done / needs_clarification / unsupported / not_an_event / failed
    spec: dict[str, object] | None = None
    detail: dict[str, object] = field(default_factory=dict)  # 澄清问题、候选、改写建议等
    steps: list[dict[str, object]] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    plan_id: str = ""  # 保存时由 store 填
    created_at: str = ""


@dataclass(frozen=True)
class RunRecord:
    """一次研究计算：研究请求、结果、当时的数据状态，以及算这一次的每一步。

    数据同步过、事件库改过之后重算的结果会不一样，所以要记下当时的数据截至哪天、事件库版本。
    """

    spec: dict[str, object]
    status: str  # done / failed
    result: dict[str, object] | None = None
    error: str | None = None  # failed 时的错误摘要
    plan_id: str | None = None
    data_through: str | None = None  # 当时本地数据截至哪天
    library_version: int | None = None  # 统计生成事件表达式时的事件库版本
    duration_ms: int | None = None
    narrative: Narrative | None = None  # 卡写过小结才有
    steps: list[dict[str, object]] = field(default_factory=list)
    run_id: str = ""  # 保存时由 store 填
    created_at: str = ""


class Store(Protocol):
    def save_plan(self, plan: PlanRecord) -> str:
        """保存一条提问记录，返回 plan_id。"""

    def get_plan(self, plan_id: str) -> PlanRecord | None:
        """取提问记录；不存在或编号不合法返回 None。"""

    def save_run(self, run: RunRecord) -> str:
        """保存一条运行记录，返回 run_id。"""

    def get_run(self, run_id: str) -> RunRecord | None:
        """取运行记录；不存在或编号不合法返回 None。"""

    def add_steps(self, record_id: str, steps: Sequence[dict[str, object]]) -> None:
        """把几步追加到提问或运行记录的过程里。编号按前缀认，没有这条记录就报错。"""

    def link_run(self, plan_id: str, run_id: str) -> None:
        """把一次运行挂到提问记录下，重复挂同一个不会记两遍。"""

    def set_narrative(self, run_id: str, narrative: Narrative) -> None:
        """把小结写进运行记录。"""
