"""LLMClient：整个项目只通过它调用大模型（ARCHITECTURE §5.1）。业务代码不感知底层是哪家。

Anthropic Messages 协议（anthropic SDK）。哪家、用什么认证头，由 `providers.py` 按环境变量定；
Anthropic 官方不填 base_url，用 SDK 自带的地址。结构化输出用强制 tool_use：定义一个叫 output 的工具，
input_schema 就是目标格式，tool_choice 强制调用，取 tool_use 块的 input。

实测（火山引擎 ark.cn-beijing.volces.com/api/coding + glm-5.3-flash）：
- 认证头：火山引擎用 Authorization: Bearer（SDK 的 auth_token），Anthropic 官方用 x-api-key（api_key）
- 返回的 content 是 [thinking, tool_use]：要找 type == "tool_use" 的块，不能取第一个
- anthropic SDK 1.5 的 messages.create 没有 temperature 参数（2026-09-15），放进请求体
- 嵌套的输出格式（对象、对象数组）能被接受，2026-09-17 实测
- glm-5.3-flash 不能关闭思考（thinking.type=disabled 返回 400），一次 12~49 秒：页面上要显示在想，超时要给够
- 同一段系统提示词连着问，后面几次的 input token 掉到几十：提示词被缓存了
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anthropic

from litmus.llm.providers import BEARER, TIMEOUT_VAR, Provider, detect, missing_message

logger = logging.getLogger(__name__)

#: 强制调用的工具名
TOOL_NAME = "output"

#: 单次输出的 token 上限：带思考时实测一次输出 2000 token 上下
MAX_TOKENS = 8000


class LLMError(RuntimeError):
    """调用大模型失败：没配、连不上、超时、认证失败、没按格式返回。"""


@dataclass(frozen=True)
class LLMConfig:
    provider: Provider
    api_key: str
    model: str
    #: 空表示用 SDK 自带的地址（Anthropic 官方就是这种）
    base_url: str = ""
    temperature: float = 0.0
    #: 带思考一次十几到一百多秒（第 7d 步实测回答追问 106 秒）
    timeout: float = 180.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LLMConfig:
        env = os.environ if env is None else env
        provider = detect(env)
        if provider is None:
            raise LLMError(missing_message())

        missing = provider.missing(env)
        if missing:
            raise LLMError(f"用{provider.label}还缺这几项，在 .env 里填：{'、'.join(missing)}")

        def read(name: str, fallback: str = "") -> str:
            return env.get(name, "").strip() or fallback

        try:
            temperature = float(read("LLM_TEMPERATURE"))
            timeout = float(read(TIMEOUT_VAR, str(cls.timeout)))
        except ValueError as exc:
            raise LLMError(f"LLM_TEMPERATURE、{TIMEOUT_VAR} 要是数字：{exc}") from exc
        return cls(
            provider=provider,
            api_key=read(provider.var("API_KEY")),
            model=read(provider.var("MODEL")),
            base_url=read(provider.var("BASE_URL")),
            temperature=temperature,
            timeout=timeout,
        )


@dataclass(frozen=True)
class StructuredReply:
    data: dict[str, Any]
    seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: 哪个模型答的。换过模型之后翻旧的过程记录要能分辨
    model: str = ""


class LLMClient(ABC):
    """P1 可加 OpenAI 兼容协议的实现（DeepSeek / 通义 / Ollama）。"""

    @abstractmethod
    def structured(self, system: str, user: str, schema: Mapping[str, Any]) -> StructuredReply:
        """按 schema 返回结构化结果。失败抛 LLMError；格式对不对（字段、表达式）由调用方检查。"""


class AnthropicClient(LLMClient):
    def __init__(self, config: LLMConfig, factory: Callable[..., Any] = anthropic.Anthropic):
        self._config = config
        self._factory = factory

    def structured(self, system: str, user: str, schema: Mapping[str, Any]) -> StructuredReply:
        config = self._config
        try:
            return self._call(system, user, schema)
        except anthropic.AuthenticationError as exc:
            raise LLMError(
                f"{config.provider.label}认证失败，检查 .env 里的 {config.provider.var('API_KEY')}"
            ) from exc

    def _call(self, system: str, user: str, schema: Mapping[str, Any]) -> StructuredReply:
        config = self._config
        credential: dict[str, Any] = (
            {"auth_token": config.api_key}
            if config.provider.auth == BEARER
            else {"api_key": config.api_key}
        )
        # SDK 默认超时、连不上时自己再试 2 次：慢的时候用户要干等三倍超时才看到失败。不让它重试，
        # 输出不对的重试由 planner 负责
        # Anthropic 官方不填 base_url，用 SDK 自带的地址
        if config.base_url:
            credential["base_url"] = config.base_url
        client = self._factory(timeout=config.timeout, max_retries=0, **credential)
        started = time.perf_counter()
        try:
            response = client.messages.create(
                model=config.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[
                    {"name": TOOL_NAME, "description": "输出结果", "input_schema": dict(schema)}
                ],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                extra_body={"temperature": config.temperature},
            )
        except anthropic.AuthenticationError:
            raise
        except anthropic.APITimeoutError as exc:
            raise LLMError(f"大模型 {config.timeout:.0f} 秒没有回应") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"连不上大模型接口：{exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"大模型接口返回 HTTP {exc.status_code}：{exc.message}") from exc
        seconds = time.perf_counter() - started
        block = next((b for b in response.content if getattr(b, "type", None) == "tool_use"), None)
        if block is None:
            raise LLMError(f"大模型没有按格式返回（stop_reason={response.stop_reason}）")
        usage = getattr(response, "usage", None)
        return StructuredReply(
            data=dict(block.input),
            seconds=seconds,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            model=config.model,
        )
