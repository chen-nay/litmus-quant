"""HTTP 请求/响应模型（ARCHITECTURE §6）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Issue(BaseModel):
    """请求里要改的一处。"""

    #: 出问题的栏目，如 "filter.expr"、"event.params.ma"；说不上是哪一栏时为空
    path: str | None = None
    message: str
    #: 可选范围，如事件参数的取值、申万行业名
    allowed: str | None = None
    #: 表达式里第几个字符出的错，从 0 数
    position: int | None = None


class AssumptionItem(BaseModel):
    """确认卡上的一条说明。"""

    #: 左边的小标题：看谁 / 看哪天 / 看哪些数 / 怎么出 / 什么事件 / 怎么算；空串是不分组的整体说明
    group: str = ""
    #: 对应的栏目，如 "when.as_of"、"scope.exclude"；整体说明为空
    field: str | None = None
    text: str
    #: 用的是默认值，确认卡标「默认值，可修改」
    default: bool = False


class CheckResponse(BaseModel):
    status: Literal["ok", "needs_revision", "data_not_ready"]
    issues: list[Issue] = Field(default_factory=list)
    #: ok：整理好的查询条件（事件按事件库生成、默认值已标出），原样交给 /api/run
    spec: dict[str, Any] | None = None
    #: ok：确认卡最上面那句「我把你的问题理解成：……」
    summary: str = ""
    #: ok：说明文字，由查询条件按模板生成
    assumptions: list[AssumptionItem] = Field(default_factory=list)
    message: str | None = None
    data: dict[str, Any] | None = None


class Candidate(BaseModel):
    """原话对应多只股票 / 多个板块时的一个候选。"""

    code: str
    name: str
    #: 股票：命中的规则（名称包含、拼音首字母、曾用名……），已退市的标出来；板块：口径
    note: str = ""
    #: 板块候选的口径：sw_industry / sw_industry_l2 / concept
    board_type: str | None = None


class Choice(BaseModel):
    """一句原话对应不止一个，要用户选一个。

    slot 说选中的填到哪：subject 是点名看的那几个（选中的代码加进 subject.codes）；
    scope 是算的范围限定的行业、板块（申万行业填 scope.industry，概念板块填 scope.board）。
    """

    slot: Literal["subject", "scope"]
    #: 用户原话里的说法：「平安」「光模块」
    mention: str
    #: 「「平安」对应 3 只股票，选一只」
    message: str
    candidates: list[Candidate]


class PlanQuestion(BaseModel):
    question: str
    options: list[str]


class PlanResponse(BaseModel):
    status: Literal[
        "ok",
        "done",
        "needs_clarification",
        "unsupported",
        "not_an_event",
        "data_not_ready",
        "failed",
    ]
    #: 这次提问的记录编号：回答追问时作为 previous_plan_id 带回来；确认卡上检查、运行时作为 plan_id 带上
    plan_id: str | None = None
    #: ok：整理好的查询条件，确认后原样交给 /api/run。
    #: 要选股票、板块或者条件要改时（needs_clarification）是草稿，改好后调 /api/check
    spec: dict[str, Any] | None = None
    #: ok：确认卡最上面那句「我把你的问题理解成：……」
    summary: str = ""
    assumptions: list[AssumptionItem] = Field(default_factory=list)
    questions: list[PlanQuestion] = Field(default_factory=list)
    #: needs_clarification：要用户选的，一句原话一组；都选完了再往下走
    choices: list[Choice] = Field(default_factory=list)
    #: done：卡不走确认卡，这里已经算完了。运行记录编号和结果同 /api/run
    run_id: str | None = None
    result: dict[str, Any] | None = None
    #: unsupported / not_an_event：系统能回答的问法，点一下当成新的提问
    alternatives: list[str] = Field(default_factory=list)
    message: str | None = None
    data: dict[str, Any] | None = None


class RunResponse(BaseModel):
    status: Literal["done", "needs_revision", "data_not_ready", "failed"]
    #: needs_revision：要改的地方，逐条列出
    issues: list[Issue] = Field(default_factory=list)
    #: done / failed：运行记录编号，GET /api/run/{run_id} 取回
    run_id: str | None = None
    #: done：表 / 卡 / 统计，按 kind 区分。卡上还带着 summary 和 assumptions（卡底下的「怎么算的」）
    result: dict[str, Any] | None = None
    #: data_not_ready / failed：给人看的一句话
    message: str | None = None
    #: data_not_ready：本地数据状态与同步进度，和 GET /api/data/status 一样
    data: dict[str, Any] | None = None


class NarrativeResponse(BaseModel):
    """卡下面的小结。text 为空时页面上这一块不显示（没什么可说、两次都没写对、大模型调不通）。"""

    text: str = ""
    #: 没写出来的原因，给开发看
    error: str | None = None
