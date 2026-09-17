"""提示词文件的加载与渲染（ARCHITECTURE §5.5）。

- 一个提示词一个文件：litmus/llm/prompts/<id>.md，第一行写 <!-- id: <id> -->，必须和文件名一致
- 占位符写成 {{变量}}：不用 $变量（和字段 $close 冲突），不用 {变量}（和 JSON 示例冲突）
- 渲染时变量不缺不多，缺了、多了都报错
- 提示词只写固定说明文字；字段清单、算子清单、事件库、默认值表由代码生成后填入
- 每次调用在日志里记 id + 内容哈希前 8 位，改过提示词能对上是哪一版
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PROMPT_DIR = Path(__file__).with_name("prompts")

_ID_LINE = re.compile(r"<!--\s*id:\s*([a-z0-9_.]+)\s*-->")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")


class PromptError(ValueError):
    """提示词文件写错了，或渲染时变量对不上。"""


@dataclass(frozen=True)
class Prompt:
    id: str
    body: str
    variables: frozenset[str]

    @property
    def version(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:8]

    def render(self, **values: object) -> str:
        missing = sorted(self.variables - values.keys())
        extra = sorted(values.keys() - self.variables)
        if missing or extra:
            raise PromptError(f"提示词 {self.id} 的变量对不上：缺 {missing}，多 {extra}")
        return _PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), self.body)


def load_prompt(prompt_id: str, directory: Path = PROMPT_DIR) -> Prompt:
    path = directory / f"{prompt_id}.md"
    if not path.is_file():
        raise PromptError(f"没有提示词文件 {path.name}")
    first, _, body = path.read_text(encoding="utf-8").partition("\n")
    match = _ID_LINE.fullmatch(first.strip())
    if match is None:
        raise PromptError(f"{path.name} 第一行要写 <!-- id: {prompt_id} -->")
    if match.group(1) != prompt_id:
        raise PromptError(f"{path.name} 里写的 id 是 {match.group(1)}，和文件名对不上")
    body = body.lstrip("\n")
    return Prompt(prompt_id, body, frozenset(_PLACEHOLDER.findall(body)))


def all_prompts(directory: Path = PROMPT_DIR) -> list[Prompt]:
    """目录里全部提示词，按 id 排。测试用它检查每个文件都能加载。"""
    return [load_prompt(path.stem, directory) for path in sorted(directory.glob("*.md"))]
