"""llm 层：只有 llm.plan()（提问 → 查询条件草稿）。

LLM 不碰数字，也不写确认卡上的说明文字——那些由 spec 用模板生成（ARCHITECTURE §5）。
其他模块只从这里 import（§1.2 第 3 条）；大模型厂商的 SDK 只能出现在这个模块里。
"""

from litmus.llm.client import AnthropicClient, LLMClient, LLMConfig, LLMError, StructuredReply
from litmus.llm.models import (
    CLARIFY,
    FAILED,
    NOT_AN_EVENT,
    OK,
    UNSUPPORTED,
    LLMCall,
    NameMention,
    PlanContext,
    PlanResult,
    PreviousTurn,
    Question,
)
from litmus.llm.planner import plan
from litmus.llm.providers import PROVIDERS, Provider
from litmus.llm.providers import detect as detect_provider

__all__ = [
    "CLARIFY",
    "FAILED",
    "NOT_AN_EVENT",
    "OK",
    "UNSUPPORTED",
    "AnthropicClient",
    "LLMCall",
    "LLMClient",
    "LLMConfig",
    "LLMError",
    "NameMention",
    "PlanContext",
    "PROVIDERS",
    "PlanResult",
    "PreviousTurn",
    "Provider",
    "Question",
    "StructuredReply",
    "detect_provider",
    "plan",
]
