"""名称解析在本地真实数据上的契约测试（第 7 步验收：「平安」返回多个候选）。

股票改名、概念板块调整之后，个别用例可能要跟着改——那正是这些测试该报出来的。没有本地数据就整个跳过。
"""

from __future__ import annotations

import pytest

from litmus.data import CONCEPT, DataService, MissingDataError
from litmus.data.resolve import CODE, FORMER, PINYIN

_ds = DataService.from_env()
try:
    _ds.data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的数据", allow_module_level=True)


def codes(text: str) -> list[str]:
    return [match.code for match in _ds.resolve_stock(text)]


def test_平安返回多个候选():
    assert {"000001.SZ", "601318.SH", "001359.SZ"} <= set(codes("平安"))


def test_名称_代码_拼音都能找到贵州茅台():
    for text in ("茅台", "贵州茅台", "600519", "GZMT"):
        assert codes(text)[0] == "600519.SH", text
    rules = {m.rule for m in _ds.resolve_stock("600519")} | {
        m.rule for m in _ds.resolve_stock("gzmt")
    }
    assert {CODE, PINYIN} <= rules


def test_简称和曾用名():
    assert "600036.SH" in codes("招行")
    assert "601857.SH" in codes("中石油")
    former = next(m for m in _ds.resolve_stock("龙净环保") if m.code == "600388.SH")
    assert former.rule == FORMER


def test_板块():
    first = _ds.resolve_board("银行")[0]
    assert (first.code, first.board_type) == ("801780.SI", "sw_industry")
    if CONCEPT in _ds.available_targets():
        assert _ds.resolve_board("光通信", "concept")[0].name == "光通信"
        assert _ds.resolve_board("光模块") == []  # 外号对不上，要靠大模型给猜测名
