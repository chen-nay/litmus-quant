"""配了哪家大模型就用哪家（ARCHITECTURE §5.1）。

环境变量分两类：

- **厂商特有**——调谁、在哪、怎么认证。每家一个前缀，按 `PROVIDERS` 的顺序找，
  第一家填了 API key 的就是要用的那家；选中之后这一组要填齐

      ARK_API_KEY=…    ARK_MODEL=…    ARK_BASE_URL=…

- **通用**——怎么调，换哪家都一样

      LLM_TEMPERATURE=…    LLM_TIMEOUT=…

**代码里不藏默认值**：模型、地址、温度都摆在 .env 里让人看见并自己填。
只有 `LLM_TIMEOUT` 不填时用 180 秒。

认证头随厂商定死：火山引擎用 `Authorization: Bearer`，Anthropic 官方用 `x-api-key`。
Anthropic 官方不填地址，用 SDK 自带的。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: 认证头
BEARER, X_API_KEY = "bearer", "x-api-key"

#: 跟厂商无关、必须填的：换哪家都要
GLOBAL_REQUIRED: tuple[str, ...] = ("LLM_TEMPERATURE",)

#: 跟厂商无关、可以不填的：超时是调优项，不填按 180 秒
TIMEOUT_VAR = "LLM_TIMEOUT"


@dataclass(frozen=True)
class Provider:
    """一家大模型的接入方式。"""

    name: str
    label: str
    #: 环境变量前缀：ARK → ARK_API_KEY / ARK_MODEL / ARK_BASE_URL / ARK_TEMPERATURE / ARK_TIMEOUT
    prefix: str
    auth: str
    #: 必须在 .env 里填的后缀。探测只看 API_KEY，选中之后这些都要齐
    required: tuple[str, ...]

    def var(self, suffix: str) -> str:
        return f"{self.prefix}_{suffix}"

    def configured(self, env: Mapping[str, str]) -> bool:
        return bool(env.get(self.var("API_KEY"), "").strip())

    def missing(self, env: Mapping[str, str]) -> list[str]:
        """选中这家之后还缺哪几个字段（含通用的）。"""
        names = [self.var(s) for s in self.required] + list(GLOBAL_REQUIRED)
        return [name for name in names if not env.get(name, "").strip()]


#: 探测顺序：往下找，第一家配了 API key 的就是它
PROVIDERS: tuple[Provider, ...] = (
    Provider(
        name="ark",
        label="火山引擎方舟",
        prefix="ARK",
        auth=BEARER,
        # 地址要填：coding plan 的 key 绑在 /api/coding 上，这条路只提供 Anthropic 协议
        required=("API_KEY", "MODEL", "BASE_URL"),
    ),
    Provider(
        name="anthropic",
        label="Anthropic",
        prefix="ANTHROPIC",
        auth=X_API_KEY,
        # 地址不填：用 anthropic SDK 自带的
        required=("API_KEY", "MODEL"),
    ),
)


def detect(env: Mapping[str, str]) -> Provider | None:
    """按顺序找第一家配了 API key 的；一家都没配返回 None。"""
    return next((provider for provider in PROVIDERS if provider.configured(env)), None)


def missing_message() -> str:
    """一家都没配时告诉用户该填什么。"""
    choices = "，或者".join(f"用{p.label}就填 {p.var('API_KEY')}" for p in PROVIDERS)
    return f"还没配大模型：在 .env 里{choices}"
