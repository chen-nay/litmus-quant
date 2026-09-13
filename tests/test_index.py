"""指数归一的测试：单位、点位、历史成分保留。小表格，不联网。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from litmus.data.loaders.index import (
    NormalizeError,
    normalize_index_daily,
    normalize_index_weight,
)


def daily_row(**overrides) -> dict:
    row = {
        "ts_code": "000300.SH",
        "trade_date": "20260911",
        "open": 3320.6898,
        "high": 3325.6070,
        "low": 3291.7842,
        "close": 3321.8248,
        "pct_chg": 0.38,
        "vol": 123456.0,  # 手
        "amount": 987654.0,  # 千元
    }
    return {**row, **overrides}


def weight_row(**overrides) -> dict:
    row = {
        "index_code": "000300.SH",
        "con_code": "000001.SZ",
        "trade_date": "20260930",
        "weight": 0.8656,
    }
    return {**row, **overrides}


# ── 指数日线 ────────────────────────────────────────────────────


def test_成交量从手换成股():
    row = normalize_index_daily([daily_row()]).row(0, named=True)
    assert row["volume"] == 123456.0 * 100


def test_成交额从千元换成元而不是万元():
    """指数日线是千元（和股票日线一样），申万行业日线才是万元。
    刚写完行业那套再来写这个，最容易顺手抄成 ×10000，差十倍。"""
    row = normalize_index_daily([daily_row()]).row(0, named=True)
    assert row["amount"] == 987654.0 * 1_000
    assert row["amount"] != 987654.0 * 10_000


def test_点位原样保留不做换算():
    """指数是点位，不是价格：不复权，也没有单位。"""
    row = normalize_index_daily([daily_row()]).row(0, named=True)
    assert row["close"] == 3321.8248
    assert row["open"] == 3320.6898


def test_指数日线日期转成日期类型():
    row = normalize_index_daily([daily_row()]).row(0, named=True)
    assert row["date"] == date(2026, 9, 11)
    assert row["code"] == "000300.SH"


def test_指数日线按日期排序():
    table = normalize_index_daily(
        [daily_row(trade_date="20260911"), daily_row(trade_date="20260901")]
    )
    assert table.get_column("date").to_list() == [date(2026, 9, 1), date(2026, 9, 11)]


def test_指数日线缺字段直接报错():
    broken = daily_row()
    del broken["amount"]
    with pytest.raises(NormalizeError, match="amount"):
        normalize_index_daily([broken])


# ── 指数成分与权重 ──────────────────────────────────────────────


def test_成分代码改名成code():
    row = normalize_index_weight([weight_row()]).row(0, named=True)
    assert row["code"] == "000001.SZ"
    assert row["index_code"] == "000300.SH"
    assert "con_code" not in row


def test_权重是百分数原样保留():
    row = normalize_index_weight([weight_row()]).row(0, named=True)
    assert row["weight"] == pytest.approx(0.8656)


def test_所有快照日都保留():
    """股票池要按当时那一期的成分算，只留最新一期就成了幸存者偏差。"""
    table = normalize_index_weight(
        [
            weight_row(trade_date="20260831"),
            weight_row(trade_date="20260930"),
        ]
    )
    assert table.height == 2
    assert table.get_column("date").to_list() == [date(2026, 8, 31), date(2026, 9, 30)]


def test_不同指数各存各的():
    table = normalize_index_weight(
        [
            weight_row(index_code="000300.SH", con_code="000001.SZ"),
            weight_row(index_code="000905.SH", con_code="000001.SZ"),
        ]
    )
    assert table.height == 2
    assert sorted(table.get_column("index_code").to_list()) == ["000300.SH", "000905.SH"]


def test_成分按指数日期代码排序():
    table = normalize_index_weight(
        [
            weight_row(con_code="600519.SH"),
            weight_row(con_code="000001.SZ"),
        ]
    )
    assert table.get_column("code").to_list() == ["000001.SZ", "600519.SH"]


def test_成分缺字段直接报错():
    broken = weight_row()
    del broken["weight"]
    with pytest.raises(NormalizeError, match="weight"):
        normalize_index_weight([broken])


# ── 通用 ────────────────────────────────────────────────────────


def test_空输入都返回空表():
    assert normalize_index_daily([]).height == 0
    assert normalize_index_weight([]).height == 0


def test_类型固定():
    daily = normalize_index_daily([daily_row()])
    assert daily.schema["date"] == pl.Date
    assert daily.schema["close"] == pl.Float64
    assert daily.schema["code"] == pl.String

    weights = normalize_index_weight([weight_row()])
    assert weights.schema["date"] == pl.Date
    assert weights.schema["weight"] == pl.Float64
