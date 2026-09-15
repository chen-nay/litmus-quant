"""llm 模块自己的数据结构（ARCHITECTURE §5.2）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from litmus.spec import Mention

OK, CLARIFY, UNSUPPORTED, NOT_AN_EVENT, FAILED = (
    "ok",
    "needs_clarification",
    "unsupported",
    "not_an_event",
    "failed",
)


@dataclass(frozen=True)
class PlanContext:
    """每次提问都给大模型的上下文，全部由代码生成。llm 不读数据，由 api 查好传进来。"""

    today: date
    latest_trading_day: date
    #: 本地股票数据从哪天起
    history_from: date
    #: 当前能用的标的类型：stock、sw_industry，概念板块可用时还有 concept
    targets: tuple[str, ...]
    #: 申万一级行业名
    industries: tuple[str, ...]
    #: 本地交易日历里最近的一段（去年 12 月起到最近一个交易日），给日期换算表数交易日
    trading_days: tuple[date, ...] = ()


@dataclass(frozen=True)
class Question:
    question: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class NameMention:
    """用户原话里的股票或概念板块，和大模型猜的全称。代码由 api 用 ds.resolve_* 核对（§2.3）。"""

    mention: str
    guess: str | None = None


@dataclass(frozen=True)
class PreviousTurn:
    """上一轮追问：原来的问题、问了什么；用户这次说的话当成回答。"""

    query: str
    questions: tuple[Question, ...]


@dataclass(frozen=True)
class PlanResult:
    status: str
    #: ok：查询条件草稿，结构同 QuerySpec；股票、概念板块还没解析成代码，没说的栏目也没补默认值
    spec: dict[str, Any] | None = None
    stock: NameMention | None = None
    board: NameMention | None = None
    mentions: tuple[Mention, ...] = ()
    questions: tuple[Question, ...] = ()
    message: str = ""
    alternatives: tuple[str, ...] = ()
    #: 调了几次大模型（格式不对会带着问题重试一次）
    attempts: int = 0
    prompt_version: str = ""
    #: failed 的原因
    error: str | None = None
