"""股票名、板块名 → 代码（ARCHITECTURE §2.3）。大模型不给代码，只给用户原话里的提及和猜测，这里确定性地查。

股票按 6 条规则查，所有命中都返回，按规则优先级排：
1. 代码：600519 / 600519.SH（不分大小写）
2. 名称完全一致
3. 名称包含：茅台 → 贵州茅台
4. 拼音首字母：gzmt → 贵州茅台。用股票列表自带的拼音列（2026-09-15 实测 5904 只里 14 只为空），不另加依赖
5. 曾用名：龙净环保 → 紫金龙净（600388.SH）
6. 字按顺序出现：招行 → 招商银行，中石油 → 中国石油

- 一只股票只出现一次，取它命中的最高一级；同一级按代码排
- 不含北交所：P0 的股票池和行情都不含（§11）；含已退市的股票，回看退市前的历史是正常需求
- 比较前统一全角转半角、去空格、字母转大写

板块按代码、名称一致、名称包含、字按顺序出现查，不做拼音（概念板块清单没有拼音列，概念名也很少有人用拼音缩写）。
「银行板块」「航运概念」这类说法去掉后缀再查一遍，取两次里更好的一级。外号对不上的（通达信没有「光模块」，
叫「光通信」「CPO概念」）由大模型给猜测名再查，这里不猜。原话和猜测名都查不到时，similar_boards 按共有的字
给几个名字相近的让用户选，不只回一句「没找到」。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

import polars as pl

CODE, NAME, CONTAINS, PINYIN, FORMER, ORDERED = 1, 2, 3, 4, 5, 6
#: 只用于板块：查不到时名字相近的
SIMILAR = 7

RULE_LABELS = {
    CODE: "代码",
    NAME: "名称",
    CONTAINS: "名称包含",
    PINYIN: "拼音首字母",
    FORMER: "曾用名",
    ORDERED: "简称",
    SIMILAR: "名字相近",
}

#: 名字相近的板块最多给几个
MAX_SIMILAR = 10

_SYMBOL = re.compile(r"\d{6}")
_BOARD_SUFFIXES = ("概念股", "概念", "板块", "行业")


@dataclass(frozen=True)
class StockMatch:
    code: str
    name: str  # 现用名
    rule: int
    matched: str  # 命中的写法：代码、现用名、拼音，或者那个曾用名
    delisted: bool

    @property
    def rule_label(self) -> str:
        return RULE_LABELS[self.rule]

    @property
    def exact(self) -> bool:
        """代码或名称完全一致。"""
        return self.rule in (CODE, NAME)


@dataclass(frozen=True)
class BoardMatch:
    code: str
    name: str
    board_type: str  # sw_industry / concept
    rule: int

    @property
    def exact(self) -> bool:
        """代码或名称完全一致。"""
        return self.rule in (CODE, NAME)


def normalize(text: str) -> str:
    """全角转半角、去空格、字母转大写。"""
    return unicodedata.normalize("NFKC", text).replace(" ", "").upper()


def _in_order(text: str, name: str) -> bool:
    rest = iter(name)
    return all(char in rest for char in text)


def match_stocks(text: str, basic: pl.DataFrame, renames: pl.DataFrame) -> list[StockMatch]:
    """basic：股票列表 (code, name, pinyin, delist_date)；renames：曾用名 (code, name)。"""
    query = normalize(text)
    if not query:
        return []
    best: dict[str, tuple[int, str]] = {}
    info: dict[str, tuple[str, bool]] = {}

    def hit(code: str, rule: int, matched: str) -> None:
        if rule < best.get(code, (ORDERED + 1, ""))[0]:
            best[code] = (rule, matched)

    for code, name, pinyin, delist_date in basic.select(
        "code", "name", "pinyin", "delist_date"
    ).iter_rows():
        if code.endswith(".BJ"):
            continue
        info[code] = (name, delist_date is not None)
        current = normalize(name or "")
        if query == code.upper() or (_SYMBOL.fullmatch(query) and code.startswith(f"{query}.")):
            hit(code, CODE, code)
        elif query == current:
            hit(code, NAME, name)
        elif query in current:
            hit(code, CONTAINS, name)
        elif pinyin and query == normalize(pinyin):
            hit(code, PINYIN, pinyin)
        elif len(query) >= 2 and _in_order(query, current):
            hit(code, ORDERED, name)

    for code, former in renames.select("code", "name").unique().iter_rows():
        if code not in info or not former:
            continue
        old = normalize(former)
        if old == normalize(info[code][0] or ""):
            continue  # 曾用名表里也有现用名那一条
        if query == old or (len(query) >= 2 and query in old):
            hit(code, FORMER, former)

    ranked = sorted(best.items(), key=lambda item: (item[1][0], item[0]))
    return [
        StockMatch(code, info[code][0], rule, matched, info[code][1])
        for code, (rule, matched) in ranked
    ]


def _board_rule(query: str, code: str, name: str) -> int | None:
    current = normalize(name)
    if query == code.upper():
        return CODE
    if query == current:
        return NAME
    if query in current:
        return CONTAINS
    if len(query) >= 2 and _in_order(query, current):
        return ORDERED
    return None


def match_boards(text: str, boards: Iterable[tuple[str, str, str]]) -> list[BoardMatch]:
    """boards：(code, name, board_type)。同一级里申万行业排在概念板块前面。"""
    query = normalize(text)
    if not query:
        return []
    queries = list(dict.fromkeys([query, _strip_suffix(query)]))
    found = []
    for code, name, board_type in boards:
        rules = [rule for q in queries if (rule := _board_rule(q, code, name)) is not None]
        if rules:
            found.append(BoardMatch(code, name, board_type, min(rules)))
    return sorted(found, key=lambda m: (m.rule, m.board_type != "sw_industry", m.code))


def similar_boards(text: str, boards: Iterable[tuple[str, str, str]]) -> list[BoardMatch]:
    """原话和猜测名都查不到时，名字相近的板块：去掉后缀后和原话共有的字越多越靠前，至少共有一半的字（最少 1 个）。

    「光模块」→ 光通信、光伏……让用户自己挑，比只回一句「没找到」好用（2026-09-15 实测本地有「光通信」「CPO概念」）。
    """
    chars = set(_strip_suffix(normalize(text)))
    if not chars:
        return []
    need = max(1, len(chars) // 2)
    scored = []
    for code, name, board_type in boards:
        shared = len(chars & set(_strip_suffix(normalize(name))))
        if shared >= need:
            scored.append(((-shared, board_type != "sw_industry", code), code, name, board_type))
    scored.sort(key=lambda item: item[0])
    return [BoardMatch(code, name, kind, SIMILAR) for _, code, name, kind in scored[:MAX_SIMILAR]]


def _strip_suffix(query: str) -> str:
    """「银行板块」→ 银行，「航运概念」→ 航运。只剩后缀本身时不去。"""
    for suffix in _BOARD_SUFFIXES:
        if query.endswith(suffix) and len(query) > len(suffix):
            return query[: -len(suffix)]
    return query
