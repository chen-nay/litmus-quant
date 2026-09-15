"""LLMClient：整个项目只通过它调用大模型（ARCHITECTURE §5.1）。业务代码不感知底层是哪家。

P0 只有 Anthropic Messages 协议（anthropic SDK，覆盖 base_url 对接火山引擎）。结构化输出用强制 tool_use：
定义一个叫 output 的工具，input_schema 就是目标格式，tool_choice 强制调用，取 tool_use 块的 input。

实测（火山引擎 ark.cn-beijing.volces.com/api/coding + glm-5.3-flash）：
- 认证头要可配：火山引擎用 Authorization: Bearer（SDK 的 auth_token），Anthropic 官方用 x-api-key（api_key）。
  默认按域名选，收到 401 自动换另一种再试一次，并在日志里提示写进 .env
- 返回的 content 是 [thinking, tool_use]：要找 type == "tool_use" 的块，不能取第一个
- anthropic SDK 1.5 的 messages.create 没有 temperature 参数（2026-09-15），放进请求体
- 扁平的输出格式（没有 $defs、oneOf）能被接受
- glm-5.3-flash 不能关闭思考（thinking.type=disabled 返回 400），一次 12~49 秒：页面上要显示在想，超时要给够
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import anthropic

logger = logging.getLogger(__name__)

AUTO, BEARER, X_API_KEY = "auto", "bearer", "x-api-key"

#: 强制调用的工具名
TOOL_NAME = "output"

#: 单次输出的 token 上限：带思考时实测一次输出 2000 token 上下
MAX_TOKENS = 8000


class LLMError(RuntimeError):
    """调用大模型失败：没配、连不上、超时、认证失败、没按格式返回。"""


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    auth_style: str = AUTO
    #: 带思考一次十几到一百多秒（第 7d 步实测回答追问 106 秒）
    timeout: float = 180.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LLMConfig:
        env = os.environ if env is None else env
        missing = [
            name for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if not env.get(name)
        ]
        if missing:
            raise LLMError(f"缺少 {'、'.join(missing)}，请在 .env 中配置")
        style = env.get("LLM_AUTH_STYLE") or AUTO
        if style not in (AUTO, BEARER, X_API_KEY):
            raise LLMError(f"LLM_AUTH_STYLE 只能是 auto、bearer、x-api-key，收到 {style!r}")
        try:
            temperature = float(env.get("LLM_TEMPERATURE") or 0)
            timeout = float(env.get("LLM_TIMEOUT") or cls.timeout)
        except ValueError as exc:
            raise LLMError(f"LLM_TEMPERATURE、LLM_TIMEOUT 要是数字：{exc}") from exc
        return cls(
            base_url=env["LLM_BASE_URL"],
            api_key=env["LLM_API_KEY"],
            model=env["LLM_MODEL"],
            temperature=temperature,
            auth_style=style,
            timeout=timeout,
        )

    def first_auth(self) -> str:
        if self.auth_style != AUTO:
            return self.auth_style
        return X_API_KEY if urlparse(self.base_url).hostname == "api.anthropic.com" else BEARER


@dataclass(frozen=True)
class StructuredReply:
    data: dict[str, Any]
    seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMClient(ABC):
    """P1 可加 OpenAI 兼容协议的实现（DeepSeek / 通义 / Ollama）。"""

    @abstractmethod
    def structured(self, system: str, user: str, schema: Mapping[str, Any]) -> StructuredReply:
        """按 schema 返回结构化结果。失败抛 LLMError；格式对不对（字段、表达式）由调用方检查。"""


class AnthropicClient(LLMClient):
    def __init__(self, config: LLMConfig, factory: Callable[..., Any] = anthropic.Anthropic):
        self._config = config
        self._factory = factory
        self._auth = config.first_auth()

    def structured(self, system: str, user: str, schema: Mapping[str, Any]) -> StructuredReply:
        try:
            return self._call(self._auth, system, user, schema)
        except anthropic.AuthenticationError as exc:
            if self._config.auth_style != AUTO:
                raise LLMError(f"大模型接口认证失败（{self._auth}），检查 LLM_API_KEY") from exc
        other = X_API_KEY if self._auth == BEARER else BEARER
        try:
            reply = self._call(other, system, user, schema)
        except anthropic.AuthenticationError as exc:
            raise LLMError(
                "大模型接口认证失败（bearer、x-api-key 都试过），检查 LLM_API_KEY"
            ) from exc
        logger.warning("认证方式换成 %s 才调通，建议在 .env 里写 LLM_AUTH_STYLE=%s", other, other)
        self._auth = other
        return reply

    def _call(
        self, auth: str, system: str, user: str, schema: Mapping[str, Any]
    ) -> StructuredReply:
        config = self._config
        credential = (
            {"auth_token": config.api_key} if auth == BEARER else {"api_key": config.api_key}
        )
        # SDK 默认超时、连不上时自己再试 2 次：慢的时候用户要干等三倍超时才看到失败。不让它重试，
        # 输出不对的重试由 planner 负责
        client = self._factory(
            base_url=config.base_url, timeout=config.timeout, max_retries=0, **credential
        )
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
        )
