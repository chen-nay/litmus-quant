"""字段目录的测试：字段查得到、按标的类型可用性正确、不可用时报错而不是糊弄。"""

from __future__ import annotations

import pytest

from litmus.data import fields
from litmus.data.fields import (
    CONCEPT,
    CONCEPT_CAPABILITY,
    FIELDS,
    INTERNAL_COLUMNS,
    STOCK,
    SW_INDUSTRY,
)
from litmus.data.manifest import Manifest


def test_按名字取字段():
    close = fields.get("close")
    assert close.unit == "元"
    assert close.dtype == "float"
    assert close.available_for(STOCK)


def test_未知字段直接报错():
    with pytest.raises(fields.UnknownFieldError, match="dealer_protest"):
        fields.get("dealer_protest")


def test_股票字段清单():
    names = fields.names_for(STOCK)
    assert "close_raw" in names
    assert "pe_ttm" in names
    assert "limit_up_num" not in names  # 板块特有


def test_概念板块字段清单():
    names = fields.names_for(CONCEPT)
    assert "limit_up_num" in names
    assert "turnover" in names
    assert "pe_ttm" not in names  # 个股估值不属于板块
    assert "market_cap" not in names  # 通达信口径只有含B股总市值，不提供


def test_申万行业没有涨停家数和换手率():
    names = fields.names_for(SW_INDUSTRY)
    assert "limit_up_num" not in names
    assert "turnover" not in names
    assert "market_cap" in names


def test_字段对该标的不可用时报错():
    with pytest.raises(fields.UnknownFieldError, match="close_raw"):
        fields.check_available(["close_raw"], CONCEPT)


def test_可用字段校验通过():
    fields.check_available(["close", "amount", "limit_up_num"], CONCEPT)


def test_不复权价禁止进时序算子():
    assert fields.get("close_raw").time_series_ok is False
    assert fields.get("close").time_series_ok is True


def test_内部列不在表达式字段里():
    """adj_factor、涨跌停价这些只给 research 用，不能出现在字段白名单中。"""
    assert not INTERNAL_COLUMNS & set(FIELDS)


def test_每个字段都填了中文名和单位():
    for name, f in FIELDS.items():
        assert f.label, f"{name} 缺中文名"
        assert f.unit, f"{name} 缺单位"
        assert f.targets, f"{name} 没写属于哪类标的"


# ── 能力探测决定哪几类标的可用 ──────────────────────────────────


def test_没探测过时概念板块不可用():
    """宁可少给一类标的，也不让上层去读一张可能没同步过的表。"""
    assert fields.available_targets(Manifest()) == (STOCK, SW_INDUSTRY)


def test_概念板块探测通过才可用():
    manifest = Manifest()
    manifest.record_capability(CONCEPT_CAPABILITY, True)

    assert fields.available_targets(manifest) == (STOCK, SW_INDUSTRY, CONCEPT)


def test_概念板块没权限时不可用_股票和申万行业不受影响():
    manifest = Manifest()
    manifest.record_capability(CONCEPT_CAPABILITY, False, "需要 6000 积分")

    assert fields.available_targets(manifest) == (STOCK, SW_INDUSTRY)
