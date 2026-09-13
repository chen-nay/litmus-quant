"""概念板块归一的测试：只留概念板块、两种量纲、字符串型的估值。小表格，不联网。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from litmus.data.loaders.concept import (
    NormalizeError,
    normalize_tdx_daily,
    normalize_tdx_index,
    normalize_tdx_member,
)


def index_row(**overrides) -> dict:
    row = {
        "ts_code": "880728.TDX",
        "trade_date": "20260911",
        "name": "航运概念",
        "idx_type": "概念板块",
        "idx_count": 64,
    }
    return {**row, **overrides}


def member_row(**overrides) -> dict:
    row = {
        "ts_code": "880728.TDX",
        "trade_date": "20260911",
        "con_code": "000039.SZ",
        "con_name": "中集集团",
    }
    return {**row, **overrides}


def daily_row(**overrides) -> dict:
    row = {
        "ts_code": "880728.TDX",
        "trade_date": "20260911",
        "open": 1234.5,
        "high": 1250.0,
        "low": 1220.0,
        "close": 1245.6,
        "pct_change": 1.23,
        "amount": 56789.0,  # 万元
        "turnover_rate": 2.5,
        "up_num": 40,
        "limit_up_num": 3,
        "pb": "2.66",  # 接口声明是字符串
        "float_mv": 1234.0,  # 亿
    }
    return {**row, **overrides}


# ── 板块清单 ────────────────────────────────────────────────────


def test_只保留概念板块():
    """接口把概念/行业/风格/地区四类混在一起返回，混进来会让板块排行变味。"""
    table = normalize_tdx_index(
        [
            index_row(ts_code="880728.TDX", idx_type="概念板块"),
            index_row(ts_code="880355.TDX", idx_type="行业板块", name="日用化工"),
            index_row(ts_code="880868.TDX", idx_type="风格板块", name="高贝塔值"),
            index_row(ts_code="880900.TDX", idx_type="地区板块", name="广东板块"),
        ]
    )
    assert table.get_column("code").to_list() == ["880728.TDX"]


def test_板块清单带成分个数():
    row = normalize_tdx_index([index_row()]).row(0, named=True)
    assert row["member_count"] == 64
    assert row["name"] == "航运概念"
    assert row["date"] == date(2026, 9, 11)


def test_板块清单缺字段直接报错():
    broken = index_row()
    del broken["idx_type"]
    with pytest.raises(NormalizeError, match="idx_type"):
        normalize_tdx_index([broken])


# ── 板块成分 ────────────────────────────────────────────────────


def test_成分区分板块代码和股票代码():
    """两个都叫 ts_code/con_code，混了就会拿板块代码去查股票。"""
    row = normalize_tdx_member([member_row()]).row(0, named=True)
    assert row["board_code"] == "880728.TDX"
    assert row["code"] == "000039.SZ"
    assert row["name"] == "中集集团"


def test_成分按板块和股票排序():
    table = normalize_tdx_member(
        [
            member_row(con_code="603967.SH"),
            member_row(con_code="000039.SZ"),
        ]
    )
    assert table.get_column("code").to_list() == ["000039.SZ", "603967.SH"]


def test_北交所成分照常保留():
    row = normalize_tdx_member([member_row(con_code="833171.BJ", con_name="国航远洋")]).row(
        0, named=True
    )
    assert row["code"] == "833171.BJ"


def test_成分缺字段直接报错():
    broken = member_row()
    del broken["con_name"]
    with pytest.raises(NormalizeError, match="con_name"):
        normalize_tdx_member([broken])


# ── 板块日线（同一个接口里两种量纲）──────────────────────────────


def test_成交额从万元换成元():
    row = normalize_tdx_daily([daily_row()]).row(0, named=True)
    assert row["amount"] == 56789.0 * 10_000


def test_流通市值从亿换成元():
    """同一个接口里 amount 是万元、float_mv 是亿，两种量纲，别混。"""
    row = normalize_tdx_daily([daily_row()]).row(0, named=True)
    assert row["circ_mv"] == 1234.0 * 100_000_000
    assert row["circ_mv"] != 1234.0 * 10_000


def test_市净率是字符串也能转成数字():
    """接口把 pe / pb 声明成 str，直接拿去比较会出错。"""
    row = normalize_tdx_daily([daily_row(pb="2.66")]).row(0, named=True)
    assert row["pb"] == pytest.approx(2.66)


def test_市净率为空时是空值():
    row = normalize_tdx_daily([daily_row(pb="")]).row(0, named=True)
    assert row["pb"] is None


def test_涨跌幅和换手率改成统一字段名():
    row = normalize_tdx_daily([daily_row()]).row(0, named=True)
    assert row["pct_chg"] == 1.23
    assert row["turnover"] == 2.5
    assert "pct_change" not in row
    assert "turnover_rate" not in row


def test_涨停家数和上涨家数保留():
    """这两个是概念板块特有的字段，申万行业没有。"""
    row = normalize_tdx_daily([daily_row()]).row(0, named=True)
    assert row["up_num"] == 40
    assert row["limit_up_num"] == 3


def test_点位不做换算():
    row = normalize_tdx_daily([daily_row()]).row(0, named=True)
    assert row["close"] == 1245.6


def test_板块日线按日期和代码排序():
    table = normalize_tdx_daily(
        [
            daily_row(trade_date="20260911", ts_code="880900.TDX"),
            daily_row(trade_date="20260910", ts_code="880728.TDX"),
        ]
    )
    assert table.get_column("date").to_list() == [date(2026, 9, 10), date(2026, 9, 11)]


def test_板块日线缺字段直接报错():
    broken = daily_row()
    del broken["limit_up_num"]
    with pytest.raises(NormalizeError, match="limit_up_num"):
        normalize_tdx_daily([broken])


# ── 通用 ────────────────────────────────────────────────────────


def test_空输入都返回空表():
    assert normalize_tdx_index([]).height == 0
    assert normalize_tdx_member([]).height == 0
    assert normalize_tdx_daily([]).height == 0


def test_类型固定():
    table = normalize_tdx_daily([daily_row()])
    assert table.schema["date"] == pl.Date
    assert table.schema["pb"] == pl.Float64
    assert table.schema["circ_mv"] == pl.Float64
