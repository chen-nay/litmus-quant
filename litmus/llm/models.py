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
    #: 申万二级行业：(上级一级行业名, 二级行业名)。二级不可用时为空
    industries_l2: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Question:
    question: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class NameMention:
    """用户原话里的股票或板块，和大模型猜的全称。代码由 api 用 ds.resolve_* 核对（§2.3）。"""

    mention: str
    guess: str | None = None
    #: mention 里放的是代码不是原话——改现有条件时大模型照抄了代码（planner.revise）。
    #: 照样走 resolve_* 核对，但确认卡上不写「「600519.SH」理解为：贵州茅台」
    is_code: bool = False


@dataclass(frozen=True)
class PreviousTurn:
    """上一轮追问：原来的问题、问了什么；用户这次说的话当成回答。"""

    query: str
    questions: tuple[Question, ...]


@dataclass(frozen=True)
class LLMCall:
    """一次大模型调用的过程：发了什么、回了什么、花了多久。由 api 存进 store 的过程记录。

    **提示词不整份存**：渲染完有几千 token（字段清单、算子清单、165 个行业名、日期换算表都在里面），
    每条提问存一份很快就几兆。存模板哈希 + 渲染后哈希足够定位是哪一版——模板在 git 里，
    变量能从当天数据重建。`prompt_version` 只是模板的哈希，同一模板在不同日期渲染出的内容不同，
    所以 `rendered_hash` 必须单独记。

    **原始返回整份存**：它小（几百 token），而且是唯一事后重建不出来的东西。
    """

    attempt: int
    prompt_id: str
    #: 提示词模板正文的哈希（Prompt.version）
    prompt_version: str
    #: 渲染后的系统提示词哈希
    rendered_hash: str
    #: 这一次发给大模型的用户侧消息（重试时是 planner.repair 渲染出来的那段）
    user_message: str
    #: 渲染后的系统提示词全文。api 只在 LITMUS_DEBUG=true 时把它写进记录
    system: str = ""
    model: str = ""
    #: 大模型返回的原始 JSON，原样存
    raw_reply: dict[str, Any] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: 读缓存的输入 token（StructuredReply.cached_tokens）
    cached_tokens: int | None = None
    seconds: float | None = None
    #: 防线②发现的问题，非空说明这次输出没通过检查，会带着问题重试
    problems: tuple[str, ...] = ()
    #: 调用失败的原因。超时、认证、连不上时 raw_reply 为空；没按格式返回时 raw_reply 是实际回的文字
    error: str | None = None


@dataclass(frozen=True)
class PlanResult:
    status: str
    #: ok：查询条件草稿，结构同 QuerySpec；点名的标的、限定的板块还没解析成代码，没说的栏目也没补默认值
    spec: dict[str, Any] | None = None
    #: 点名看的标的（subject），按 scope.target 是股票还是板块去查
    subjects: tuple[NameMention, ...] = ()
    #: 算的范围限定在哪个行业、板块（scope.board）：申万行业和通达信概念一起查
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
    #: 每次大模型调用的过程，给 store 的过程记录用（一次重试就有两条）
    calls: tuple[LLMCall, ...] = ()
