"""LLMClient 与提示词加载的离线测试：用假的 SDK，不发请求。"""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from litmus.llm.client import AnthropicClient, LLMConfig, LLMError, LLMFormatError
from litmus.llm.prompts import PromptError, load_prompt
from litmus.llm.providers import PROVIDERS, detect, missing_message

SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}
ARK, ANTHROPIC = PROVIDERS
VOLCES = "https://ark.cn-beijing.volces.com/api/coding"
#: 一组填齐的火山引擎配置（厂商特有 + 通用）
ARK_ENV = {
    "ARK_API_KEY": "k",
    "ARK_MODEL": "m",
    "ARK_BASE_URL": VOLCES,
    "LLM_TEMPERATURE": "0",
}


def config(provider=ARK, **changes) -> LLMConfig:
    base = VOLCES if provider is ARK else ""
    values = {"api_key": "k", "model": "m", "base_url": base, **changes}
    return LLMConfig(provider=provider, **values)


def reply(data=None, blocks=("thinking", "tool_use"), stop_reason="tool_use"):
    made = {
        "tool_use": lambda: SimpleNamespace(type="tool_use", input=data or {"status": "ok"}),
        "thinking": lambda: SimpleNamespace(type="thinking", thinking="先想一想"),
        "text": lambda: SimpleNamespace(type="text", text="这个问题我直接回答："),
    }
    return SimpleNamespace(
        content=[made[kind]() for kind in blocks],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=3, output_tokens=5, cache_read_input_tokens=8000),
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


def test_一家都没配时说清该填什么():
    assert detect({}) is None
    assert detect({"ARK_API_KEY": "   "}) is None  # 空白不算配了
    with pytest.raises(LLMError, match="ARK_API_KEY"):
        LLMConfig.from_env({})
    assert "ANTHROPIC_API_KEY" in missing_message()


def test_按顺序找_第一家配了API_KEY的就是它():
    assert detect({"ARK_API_KEY": "k"}) is ARK
    assert detect({"ANTHROPIC_API_KEY": "k"}) is ANTHROPIC
    # 两家都配了，按 PROVIDERS 的顺序取前面那家
    assert detect({"ANTHROPIC_API_KEY": "k", "ARK_API_KEY": "k"}) is ARK


def test_选中一家之后这组字段要填齐_缺了说清缺哪个():
    # 厂商特有的和通用的一起报
    with pytest.raises(LLMError, match="ARK_MODEL、ARK_BASE_URL、LLM_TEMPERATURE"):
        LLMConfig.from_env({"ARK_API_KEY": "k"})
    with pytest.raises(LLMError, match="LLM_TEMPERATURE"):
        LLMConfig.from_env({**ARK_ENV, "LLM_TEMPERATURE": "  "})
    # Anthropic 官方不填地址，所以它不在必填里
    with pytest.raises(LLMError, match="ANTHROPIC_MODEL、LLM_TEMPERATURE"):
        LLMConfig.from_env({"ANTHROPIC_API_KEY": "k"})
    assert "BASE_URL" not in ANTHROPIC.required


def test_温度和超时是通用的_换哪家都一样():
    for env in (
        ARK_ENV,
        {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "m", "LLM_TEMPERATURE": "0.7"},
    ):
        assert LLMConfig.from_env({**env, "LLM_TEMPERATURE": "0.7"}).temperature == 0.7
        assert LLMConfig.from_env({**env, "LLM_TIMEOUT": "90"}).timeout == 90.0
    # 只有超时不填时有默认
    assert LLMConfig.from_env(ARK_ENV).timeout == 180.0


def test_配置全部从环境变量读_代码里不藏默认值():
    loaded = LLMConfig.from_env({**ARK_ENV, "ARK_MODEL": "别的模型"})
    assert (loaded.provider, loaded.api_key) == (ARK, "k")
    assert (loaded.model, loaded.base_url) == ("别的模型", VOLCES)

    official = LLMConfig.from_env(
        {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "claude-opus-5", "LLM_TEMPERATURE": "0"}
    )
    assert official.provider is ANTHROPIC and official.base_url == ""  # 用 SDK 自带的地址


def test_温度和超时写成非数字时报错():
    with pytest.raises(LLMError, match="LLM_TEMPERATURE"):
        LLMConfig.from_env({**ARK_ENV, "LLM_TEMPERATURE": "很高"})


def test_认证头随厂商定死():
    assert (ARK.auth, ANTHROPIC.auth) == ("bearer", "x-api-key")


# ── 调用 ────────────────────────────────────────────────────────


def test_强制调用工具_跳过思考块取结构化结果_温度放进请求体():
    sdk = FakeSDK(reply({"status": "ok", "shape": "stock_list"}))
    result = AnthropicClient(config(), factory=sdk).structured("系统", "问题", SCHEMA)
    assert result.data == {"status": "ok", "shape": "stock_list"}
    assert (result.input_tokens, result.cached_tokens, result.output_tokens) == (3, 8000, 5)
    call = sdk.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "output"}
    assert call["tools"][0]["input_schema"] == SCHEMA
    assert call["extra_body"] == {"temperature": 0.0}
    assert sdk.clients[0]["auth_token"] == "k" and "api_key" not in sdk.clients[0]
    assert sdk.clients[0]["base_url"] == VOLCES
    assert sdk.clients[0]["max_retries"] == 0  # SDK 自己不重试，超时了马上告诉用户


def test_没有返回工具调用就报错_实际回的文字和token带在错误里():
    sdk = FakeSDK(reply(blocks=("thinking", "text"), stop_reason="end_turn"))
    with pytest.raises(LLMFormatError, match="没有按格式返回") as caught:
        AnthropicClient(config(), factory=sdk).structured("s", "u", SCHEMA)
    got = caught.value.reply
    assert got is not None
    assert got.data == {
        "stop_reason": "end_turn",
        "text": "这个问题我直接回答：",
        "thinking_chars": 4,
    }
    assert (got.input_tokens, got.cached_tokens, got.output_tokens) == (3, 8000, 5)


def test_Anthropic官方不传base_url_用SDK自带的地址():
    sdk = FakeSDK(reply())
    AnthropicClient(config(ANTHROPIC), factory=sdk).structured("s", "u", SCHEMA)
    assert "base_url" not in sdk.clients[0]
    assert sdk.clients[0]["api_key"] == "k" and "auth_token" not in sdk.clients[0]


def test_认证失败时说清检查哪个环境变量():
    sdk = FakeSDK(auth_error())
    with pytest.raises(LLMError, match="火山引擎方舟认证失败.*ARK_API_KEY"):
        AnthropicClient(config(), factory=sdk).structured("s", "u", SCHEMA)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicClient(config(ANTHROPIC), factory=FakeSDK(auth_error())).structured(
            "s", "u", SCHEMA
        )


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
