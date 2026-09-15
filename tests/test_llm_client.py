"""LLMClient 与提示词加载的离线测试：用假的 SDK，不发请求。"""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from litmus.llm.client import AnthropicClient, LLMConfig, LLMError
from litmus.llm.prompts import PromptError, load_prompt

SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}
VOLCES = "https://ark.cn-beijing.volces.com/api/coding"


def config(**changes) -> LLMConfig:
    values = {"base_url": VOLCES, "api_key": "k", "model": "m", **changes}
    return LLMConfig(**values)


def reply(data=None, blocks=("thinking", "tool_use")):
    content = [
        SimpleNamespace(type=kind, input=data or {"status": "ok"})
        if kind == "tool_use"
        else SimpleNamespace(type=kind)
        for kind in blocks
    ]
    return SimpleNamespace(
        content=content,
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=3, output_tokens=5),
    )


def auth_error() -> anthropic.AuthenticationError:
    request = httpx.Request("POST", VOLCES)
    return anthropic.AuthenticationError(
        "401", response=httpx.Response(401, request=request), body=None
    )


class FakeSDK:
    """记下每次构造用的参数和 create 的参数；outcomes 依次返回或抛出。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.clients: list[dict] = []
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.clients.append(kwargs)
        return SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# ── 配置 ────────────────────────────────────────────────────────


def test_配置缺项时说清缺什么():
    with pytest.raises(LLMError, match="LLM_API_KEY、LLM_MODEL"):
        LLMConfig.from_env({"LLM_BASE_URL": VOLCES})


def test_配置从环境变量读():
    loaded = LLMConfig.from_env(
        {"LLM_BASE_URL": VOLCES, "LLM_API_KEY": "k", "LLM_MODEL": "m", "LLM_TIMEOUT": "90"}
    )
    assert (loaded.temperature, loaded.auth_style, loaded.timeout) == (0.0, "auto", 90.0)
    unset = LLMConfig.from_env({"LLM_BASE_URL": VOLCES, "LLM_API_KEY": "k", "LLM_MODEL": "m"})
    assert unset.timeout == 180.0
    with pytest.raises(LLMError, match="LLM_AUTH_STYLE"):
        LLMConfig.from_env(
            {
                "LLM_BASE_URL": VOLCES,
                "LLM_API_KEY": "k",
                "LLM_MODEL": "m",
                "LLM_AUTH_STYLE": "basic",
            }
        )


def test_认证方式默认按域名选():
    assert config().first_auth() == "bearer"
    assert config(base_url="https://api.anthropic.com").first_auth() == "x-api-key"
    assert config(auth_style="x-api-key").first_auth() == "x-api-key"


# ── 调用 ────────────────────────────────────────────────────────


def test_强制调用工具_跳过思考块取结构化结果_温度放进请求体():
    sdk = FakeSDK(reply({"status": "ok", "shape": "stock_list"}))
    result = AnthropicClient(config(), factory=sdk).structured("系统", "问题", SCHEMA)
    assert result.data == {"status": "ok", "shape": "stock_list"}
    assert (result.input_tokens, result.output_tokens) == (3, 5)
    call = sdk.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "output"}
    assert call["tools"][0]["input_schema"] == SCHEMA
    assert call["extra_body"] == {"temperature": 0.0}
    assert sdk.clients[0]["auth_token"] == "k" and "api_key" not in sdk.clients[0]
    assert sdk.clients[0]["max_retries"] == 0  # SDK 自己不重试，超时了马上告诉用户


def test_没有返回工具调用就报错():
    sdk = FakeSDK(reply(blocks=("thinking", "text")))
    with pytest.raises(LLMError, match="没有按格式返回"):
        AnthropicClient(config(), factory=sdk).structured("s", "u", SCHEMA)


def test_401时自动换另一种认证_换成功就记住():
    sdk = FakeSDK(auth_error(), reply(), reply())
    client = AnthropicClient(config(), factory=sdk)
    client.structured("s", "u", SCHEMA)
    client.structured("s", "u", SCHEMA)
    assert ["auth_token" in c for c in sdk.clients] == [True, False, False]


def test_明确指定认证方式时401直接报错():
    sdk = FakeSDK(auth_error())
    with pytest.raises(LLMError, match="认证失败"):
        AnthropicClient(config(auth_style="bearer"), factory=sdk).structured("s", "u", SCHEMA)


def test_超时说清等了多久():
    sdk = FakeSDK(anthropic.APITimeoutError(request=httpx.Request("POST", VOLCES)))
    with pytest.raises(LLMError, match="180 秒没有回应"):
        AnthropicClient(config(), factory=sdk).structured("s", "u", SCHEMA)


# ── 提示词文件 ──────────────────────────────────────────────────


def write(tmp_path, name, text):
    (tmp_path / f"{name}.md").write_text(text, encoding="utf-8")


def test_提示词_渲染和变量检查(tmp_path):
    write(
        tmp_path,
        "demo.hello",
        '<!-- id: demo.hello -->\n\n今天 {{today}}，字段 $close，示例 {"a": 1}，{{ today }}\n',
    )
    prompt = load_prompt("demo.hello", tmp_path)
    assert prompt.variables == {"today"}
    assert (
        prompt.render(today="2026-09-15")
        == '今天 2026-09-15，字段 $close，示例 {"a": 1}，2026-09-15\n'
    )
    assert len(prompt.version) == 8
    with pytest.raises(PromptError, match=r"缺 \['today'\]"):
        prompt.render()
    with pytest.raises(PromptError, match=r"多 \['extra'\]"):
        prompt.render(today="x", extra="y")


def test_提示词_id和文件名要一致(tmp_path):
    write(tmp_path, "demo.a", "<!-- id: demo.b -->\n正文\n")
    with pytest.raises(PromptError, match="对不上"):
        load_prompt("demo.a", tmp_path)
    write(tmp_path, "demo.c", "正文\n")
    with pytest.raises(PromptError, match="第一行"):
        load_prompt("demo.c", tmp_path)
    with pytest.raises(PromptError, match="没有提示词文件"):
        load_prompt("demo.missing", tmp_path)
