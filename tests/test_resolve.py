"""名称解析的规则（ARCHITECTURE §2.3）：小表格逐条核对，不读本地数据。真实数据上的见 tests/contract/test_resolve.py。"""

from __future__ import annotations

from datetime import date

import polars as pl

from litmus.data.resolve import (
    CODE,
    CONTAINS,
    FORMER,
    NAME,
    ORDERED,
    PINYIN,
    SIMILAR,
    match_boards,
    match_stocks,
    similar_boards,
)

BASIC = pl.DataFrame(
    [
        ("600519.SH", "贵州茅台", "GZMT", None),
        ("600036.SH", "招商银行", "ZSYH", None),
        ("601857.SH", "中国石油", "ZGSY", None),
        ("603619.SH", "中曼石油", "ZMSY", None),
        ("000001.SZ", "平安银行", "PAYH", None),
        ("601318.SH", "中国平安", "ZGPA", None),
        ("600388.SH", "紫金龙净", "ZJLJ", None),
        ("000005.SZ", "ST星源(退)", "STXY", date(2024, 4, 26)),
        ("832317.BJ", "观典防务", "GDFW", None),
    ],
    schema={"code": pl.String, "name": pl.String, "pinyin": pl.String, "delist_date": pl.Date},
    orient="row",
)
RENAMES = pl.DataFrame(
    [("600388.SH", "龙净环保"), ("600388.SH", "紫金龙净")],
    schema={"code": pl.String, "name": pl.String},
    orient="row",
)

BOARDS = [
    ("801780.SI", "银行", "sw_industry"),
    ("801080.SI", "电子", "sw_industry"),
    ("880728.TDX", "航运概念", "concept"),
    ("880500.TDX", "光通信", "concept"),
    ("880501.TDX", "存储芯片", "concept"),
    ("880502.TDX", "芯片", "concept"),
    ("880503.TDX", "电子烟", "concept"),
]


def stocks(text: str) -> list[tuple[str, int]]:
    return [(match.code, match.rule) for match in match_stocks(text, BASIC, RENAMES)]


def boards(text: str) -> list[tuple[str, int]]:
    return [(match.code, match.rule) for match in match_boards(text, BOARDS)]


# ── 股票 ────────────────────────────────────────────────────────


def test_代码_带不带后缀_不分大小写():
    assert stocks("600519") == [("600519.SH", CODE)]
    assert stocks("600519.sh") == [("600519.SH", CODE)]


def test_名称完全一致():
    assert stocks("中国平安") == [("601318.SH", NAME)]


def test_名称包含_同样包含的一并返回_按代码排():
    assert stocks("平安") == [("000001.SZ", CONTAINS), ("601318.SH", CONTAINS)]


def test_拼音首字母():
    assert stocks("gzmt") == [("600519.SH", PINYIN)]


def test_曾用名_返回现用名和命中的曾用名():
    [match] = match_stocks("龙净环保", BASIC, RENAMES)
    assert (match.code, match.name, match.rule, match.matched) == (
        "600388.SH",
        "紫金龙净",
        FORMER,
        "龙净环保",
    )


def test_字按顺序出现():
    assert stocks("招行") == [("600036.SH", ORDERED)]
    assert stocks("中石油") == [("601857.SH", ORDERED), ("603619.SH", ORDERED)]


def test_一只股票只出现一次_曾用名表里的现用名不重复算():
    assert stocks("紫金龙净") == [("600388.SH", NAME)]


def test_不含北交所_含已退市并标出来():
    assert stocks("观典防务") == []
    [match] = match_stocks("星源", BASIC, RENAMES)
    assert (match.rule, match.delisted) == (CONTAINS, True)


def test_全角字母_空格_空文本():
    assert stocks(" ＧＺＭＴ ") == [("600519.SH", PINYIN)]
    assert stocks("   ") == []


# ── 板块 ────────────────────────────────────────────────────────


def test_板块_去掉概念板块行业这类后缀再查():
    assert boards("银行板块") == [("801780.SI", NAME)]
    assert boards("航运概念") == [("880728.TDX", NAME)]
    assert boards("航运") == [("880728.TDX", CONTAINS)]


def test_板块_名称一致排前面_同一级申万排在概念前面():
    assert boards("芯片") == [("880502.TDX", NAME), ("880501.TDX", CONTAINS)]
    assert boards("电子") == [("801080.SI", NAME), ("880503.TDX", CONTAINS)]


def test_板块_代码_外号查不到():
    assert boards("880728.tdx") == [("880728.TDX", CODE)]
    assert boards("光模块") == []  # 通达信叫「光通信」：要靠大模型给猜测名


def test_板块_查不到时给名字相近的_共有的字多的排前面():
    def similar(text: str) -> list[tuple[str, int]]:
        return [(match.code, match.rule) for match in similar_boards(text, BOARDS)]

    assert similar("光模块") == [("880500.TDX", SIMILAR)]  # 共有「光」
    # 5 个字至少共有 2 个：存储芯片共有 4 个、芯片 2 个
    assert similar("芯片存储器") == [("880501.TDX", SIMILAR), ("880502.TDX", SIMILAR)]
    assert similar("电子烟草概念") == [("880503.TDX", SIMILAR), ("801080.SI", SIMILAR)]
    assert similar("黄金") == []
