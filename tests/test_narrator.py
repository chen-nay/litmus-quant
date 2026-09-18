"""卡下面那段话（DESIGN.md §1.6）：假的大模型客户端，核对「话里不许有数字」的校验和重试。不发请求。"""

from __future__ import annotations

from litmus.llm import LLMClient, LLMError, LLMFormatError, StructuredReply, narrate
from litmus.llm.narrator import MAX_CHARS, card_text, check
from litmus.llm.prompts import load_prompt

CARD = {
    "kind": "card",
    "items": [
        {
            "code": "002714.SZ",
            "name": "牧原股份",
            "industry": "农林牧渔 / 养殖业",
            "rows": [
                {"name": "市盈率TTM", "text": "无", "note": "2026 上半年亏损，亏损股没有市盈率"},
                {"name": "市净率", "text": "3.00", "note": "两年分位 28%，偏低"},
                {"name": "换手率", "text": "1.49%", "note": ""},
            ],
        }
    ],
}
GOOD = "上半年亏损，市盈率看不了；市净率在两年里偏低。"


class FakeClient(LLMClient):
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def structured(self, system, user, schema):
        self.calls.append(user)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return StructuredReply({"text": outcome}, 0.1)


def say(*outcomes, question="牧原股份最近走势如何？"):
    client = FakeClient(*outcomes)
    return narrate(CARD, question, client), client


def test_交给大模型的是问题和卡上的文字():
    text = card_text(CARD, "牧原股份最近走势如何？")
    assert text.startswith("问题：牧原股份最近走势如何？\n卡：\n牧原股份（农林牧渔 / 养殖业）")
    assert "- 市盈率TTM 无（2026 上半年亏损，亏损股没有市盈率）" in text
    assert "- 换手率 1.49%\n" not in text and text.endswith("- 换手率 1.49%")  # 没有解释行不加括号
    assert not card_text(CARD, None).startswith("问题")  # 表单直接提交的没有原话


def test_提示词能渲染_写明不许有数字():
    system = load_prompt("narrator.system").render(max_chars=MAX_CHARS)
    assert "不写任何数字" in system and f"不超过 {MAX_CHARS} 个字" in system


def test_一段不含数字的话_直接用():
    narration, client = say(GOOD)
    assert (narration.text, narration.error) == (GOOD, None)
    assert len(client.calls) == 1 and len(narration.calls) == 1
    assert narration.calls[0].raw_reply == {"text": GOOD}


def test_故意写数字_校验拦住_带着问题重试():
    narration, client = say("市净率 3.00，两年分位 28%，偏低。", GOOD)
    assert narration.text == GOOD
    retry = client.calls[1]
    assert "你上一次写的是：市净率 3.00，两年分位 28%，偏低。" in retry
    assert "话里出现了数字或百分号" in retry
    assert narration.calls[0].problems and not narration.calls[1].problems


def test_两次都写了数字_不给话():
    narration, client = say("跌了 17.89%", "排第 94 名")
    assert narration.text == "" and len(client.calls) == 2
    assert "数字" in narration.error


def test_全角数字和百分号也拦():
    assert check("跌了１０％")
    assert check("跌了十%")


def test_用汉字写的数值也拦_说哪一段的不拦():
    """2026-09-18 实测：用户要「用汉字说」时，大模型写了「跌了百分之十七点八九」。"""
    for text in (
        "今年以来跌了百分之十七点八九",
        "市净率三点零",
        "排在第九十四名",
        "市净率是行业的两倍",
        "跌幅差了三个点",
        "比行业多跌三个百分点",
    ):
        assert check(text), text
    for text in (
        "两年分位偏低",
        "上半年亏损",
        "近二十个交易日在涨",
        "一季度",
        "今年以来跌得比行业多",
    ):
        assert check(text) == [], text


def test_太长也重试():
    assert check("长" * (MAX_CHARS + 1))
    narration, _ = say("长" * (MAX_CHARS + 1), GOOD)
    assert narration.text == GOOD


def test_没什么可说_返回空串也行():
    narration, _ = say("")
    assert (narration.text, narration.error) == ("", None)


def test_没调用工具_原样再问一次():
    narration, client = say(LLMFormatError("大模型没有按格式返回（stop_reason=end_turn）"), GOOD)
    assert narration.text == GOOD and client.calls[0] == client.calls[1]


def test_调用失败_不给话_说明原因():
    narration, _ = say(LLMError("大模型 180 秒没有回应"))
    assert (narration.text, narration.error) == ("", "大模型 180 秒没有回应")
