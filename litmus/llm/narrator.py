"""卡下面那段话（DESIGN.md §1.6）：拿卡上的内容让大模型串一段话。

- **话里不写数字**：数字都在卡上，话只负责把它们连起来。出现阿拉伯数字、百分号，或者用汉字写的数值
  （百分之十七、第九十四名），就带着问题重试一次，
  还不行就不给话——卡照样出，话是卡的附属品
- 只给大模型卡上的文字（值和解释行），不给原始数据：它能说的只有卡上已有的东西
- 数字保证跑一万次相同，话不保证逐字相同，所以话跟着这一次运行存下来，不重新生成
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from litmus.llm.client import LLMClient, LLMError, LLMFormatError, reply_fields
from litmus.llm.models import LLMCall
from litmus.llm.prompts import load_prompt

logger = logging.getLogger(__name__)

#: 话最长多少个字
MAX_CHARS = 120

#: 阿拉伯数字（半角、全角）和百分号
_NUMBERS = re.compile(r"[0-9０-９%％]")

#: 用汉字写的数值：百分之十七、十七点八九、第九十四名、两倍、差三个点。
#: 「两年」「上半年」「近二十个交易日」是说哪一段，不是卡上的数值，不拦。
#: 2026-09-18 实测：用户要「用汉字说」时，大模型写了「跌了百分之十七点八九」
_DIGIT = "零〇一二三四五六七八九十百千万两"
_SPELLED = re.compile(
    rf"百分之[{_DIGIT}]|[{_DIGIT}]+点[{_DIGIT}]|第[{_DIGIT}]+[名位]|[{_DIGIT}]+(?:倍|成|个百分点|个点)"
)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "卡下面那段话，不写任何数字；没什么可说就返回空字符串",
        }
    },
    "required": ["text"],
}


@dataclass(frozen=True)
class Narration:
    #: 那段话；没写、写不出来都是空串
    text: str
    #: 每次调用的过程，给过程记录用（重试就有两条）
    calls: tuple[LLMCall, ...] = ()
    #: 没写出来的原因
    error: str | None = None


def narrate(card: Mapping[str, Any], question: str | None, client: LLMClient) -> Narration:
    """card：卡的结果（research.CardResult 转成的 dict）。question：用户原话，表单直接提交的没有。"""
    prompt = load_prompt("narrator.system")
    system = prompt.render(max_chars=MAX_CHARS)
    rendered = hashlib.sha256(system.encode("utf-8")).hexdigest()[:8]
    user = card_text(card, question)
    message, problems, text = user, [], ""
    calls: list[LLMCall] = []
    for attempt in (1, 2):
        if problems:
            message = (
                f"{user}\n\n你上一次写的是：{text}\n不行，因为：{'；'.join(problems)}。请重写。"
            )
        try:
            reply = client.structured(system, message, OUTPUT_SCHEMA)
        except LLMFormatError as exc:
            # 没调用工具就没有话可改，原样再问一次
            calls.append(
                _call(prompt, rendered, attempt, message, error=str(exc), **reply_fields(exc.reply))
            )
            problems = []
            if attempt == 1:
                continue
            return Narration("", tuple(calls), str(exc))
        except LLMError as exc:
            calls.append(_call(prompt, rendered, attempt, message, error=str(exc)))
            logger.warning("narrator 第 %d 次调用失败：%s", attempt, exc)
            return Narration("", tuple(calls), str(exc))
        text = reply.data.get("text") if isinstance(reply.data.get("text"), str) else ""
        text = text.strip()
        problems = check(text)
        calls.append(
            _call(
                prompt,
                rendered,
                attempt,
                message,
                **reply_fields(reply),
                problems=tuple(problems),
            )
        )
        if not problems:
            return Narration(text, tuple(calls))
    return Narration("", tuple(calls), "；".join(problems))


def check(text: str) -> list[str]:
    """话里不许有的东西。空串是允许的（没什么可说）。"""
    problems = []
    if found := sorted(set(_NUMBERS.findall(text))):
        problems.append(
            f"话里出现了数字或百分号（{' '.join(found)}），数字都在卡上，话里一个都不写"
        )
    if spelled := _SPELLED.findall(text):
        problems.append(
            f"话里用汉字写了数值（{'、'.join(dict.fromkeys(spelled))}），写成汉字也不行，"
            "用「跌得比行业多」「偏低」这类说法"
        )
    if len(text) > MAX_CHARS:
        problems.append(f"太长了（{len(text)} 个字），不超过 {MAX_CHARS} 个字")
    return problems


def card_text(card: Mapping[str, Any], question: str | None) -> str:
    """交给大模型的卡：每个标的一段，每个指标一行「名字 值（解释行）」。"""
    lines = []
    for item in card.get("items", []):
        head = f"{item.get('name') or item.get('code')}（{item.get('industry')}）"
        lines.append(head if item.get("industry") else str(item.get("name") or item.get("code")))
        for row in item.get("rows", []):
            note = f"（{row['note']}）" if row.get("note") else ""
            lines.append(f"- {row['name']} {row['text']}{note}")
    card_lines = "\n".join(lines)
    asked = f"问题：{question}\n" if question else ""
    return f"{asked}卡：\n{card_lines}"


def _call(prompt, rendered: str, attempt: int, message: str, **extra: object) -> LLMCall:
    return LLMCall(
        attempt=attempt,
        prompt_id=prompt.id,
        prompt_version=prompt.version,
        rendered_hash=rendered,
        user_message=message,
        **extra,  # type: ignore[arg-type]
    )
