"""申万行业归一的测试：单位换算、时间区间归属、只留一级行业。小表格，不联网。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from litmus.data.loaders.industry import (
    NormalizeError,
    normalize_index_classify,
    normalize_industry_member,
    normalize_sw_daily,
)


def classify_row(**overrides) -> dict:
    row = {
        "index_code": "801010.SI",
        "industry_name": "农林牧渔",
        "level": "L1",
        "parent_code": "0",
        "src": "SW2021",
    }
    return {**row, **overrides}


def member_row(**overrides) -> dict:
    row = {
        "l1_code": "801050.SI",
        "l1_name": "有色金属",
        "ts_code": "600547.SH",
        "in_date": "20030826",
        "out_date": None,
    }
    return {**row, **overrides}


def sw_row(**overrides) -> dict:
    row = {
        "ts_code": "801010.SI",
        "trade_date": "20260911",
        "name": "农林牧渔",
        "open": 2986.75,
        "high": 3000.0,
        "low": 2940.0,
        "close": 2946.60,
        "pct_change": -1.34,
        "amount": 83532.0,  # 万元
        "pb": 2.66,
        "float_mv": 500000.0,  # 万元
        "total_mv": 800000.0,  # 万元
    }
    return {**row, **overrides}


# ── 行业清单 ────────────────────────────────────────────────────


def test_行业代码和名称改名():
    row = normalize_index_classify([classify_row()]).row(0, named=True)
    assert row["code"] == "801010.SI"
    assert row["name"] == "农林牧渔"


def test_只保留一级行业():
    """接口一次会返回 L1/L2/L3，P0 只用一级。"""
    table = normalize_index_classify(
        [
            classify_row(index_code="801010.SI", level="L1"),
            classify_row(index_code="801011.SI", level="L2", industry_name="种植业"),
            classify_row(index_code="850111.SI", level="L3", industry_name="粮食种植"),
        ]
    )
    assert table.get_column("code").to_list() == ["801010.SI"]


def test_行业清单按代码排序():
    table = normalize_index_classify(
        [classify_row(index_code="801050.SI"), classify_row(index_code="801010.SI")]
    )
    assert table.get_column("code").to_list() == ["801010.SI", "801050.SI"]


def test_行业清单缺字段直接报错():
    broken = classify_row()
    del broken["industry_name"]
    with pytest.raises(NormalizeError, match="industry_name"):
        normalize_index_classify([broken])


# ── 行业归属（时间区间）──────────────────────────────────────────


def test_纳入日期转成日期类型():
    row = normalize_industry_member([member_row()]).row(0, named=True)
    assert row["in_date"] == date(2003, 8, 26)
    assert row["industry_code"] == "801050.SI"


def test_剔除日期为空表示至今仍属于该行业():
    row = normalize_industry_member([member_row()]).row(0, named=True)
    assert row["out_date"] is None


def test_已经调出的股票带剔除日期():
    row = normalize_industry_member([member_row(out_date="20220729")]).row(0, named=True)
    assert row["out_date"] == date(2022, 7, 29)


def test_同一只股票的多段归属都保留():
    """换过行业的股票有多段区间，判断某天归属要靠区间覆盖，不能只留一条。"""
    table = normalize_industry_member(
        [
            member_row(
                l1_code="801050.SI", l1_name="有色金属", in_date="20030826", out_date="20220728"
            ),
            member_row(l1_code="801080.SI", l1_name="电子", in_date="20220729", out_date=None),
        ]
    )
    assert table.height == 2
    assert table.get_column("industry_name").to_list() == ["有色金属", "电子"]


def test_一级相同三级不同的行会塌成一行():
    """只取一级行业的列之后，三级行业变动但一级没变的记录就是同一行。"""
    table = normalize_industry_member([member_row(), member_row()])
    assert table.height == 1


def test_归属按代码和纳入日排序():
    table = normalize_industry_member(
        [
            member_row(ts_code="600547.SH", in_date="20030826"),
            member_row(ts_code="000001.SZ", in_date="19910403"),
            member_row(ts_code="600547.SH", in_date="19990101"),
        ]
    )
    assert table.get_column("code").to_list() == ["000001.SZ", "600547.SH", "600547.SH"]
    assert table.get_column("in_date").to_list()[1:] == [date(1999, 1, 1), date(2003, 8, 26)]


def test_归属缺字段直接报错():
    broken = member_row()
    del broken["in_date"]
    with pytest.raises(NormalizeError, match="in_date"):
        normalize_industry_member([broken])


# ── 行业日线（单位与股票日线不同）────────────────────────────────


def test_成交额从万元换成元():
    """股票日线的 amount 是千元，行业日线是万元——照搬会差十倍。"""
    row = normalize_sw_daily([sw_row()]).row(0, named=True)
    assert row["amount"] == 83532.0 * 10_000


def test_市值从万元换成元():
    row = normalize_sw_daily([sw_row()]).row(0, named=True)
    assert row["market_cap"] == 800000.0 * 10_000
    assert row["circ_mv"] == 500000.0 * 10_000


def test_涨跌幅字段名和股票日线不同():
    """这里叫 pct_change，股票日线叫 pct_chg，归一后统一成 pct_chg。"""
    row = normalize_sw_daily([sw_row()]).row(0, named=True)
    assert row["pct_chg"] == -1.34
    assert "pct_change" not in row


def test_点位原样保留不做换算():
    """行业指数是点位，不是价格，没有复权也没有单位换算。"""
    row = normalize_sw_daily([sw_row()]).row(0, named=True)
    assert row["close"] == 2946.60
    assert row["open"] == 2986.75


def test_行业日线带行业名字():
    row = normalize_sw_daily([sw_row()]).row(0, named=True)
    assert row["name"] == "农林牧渔"
    assert row["code"] == "801010.SI"


def test_行业日线按日期和代码排序():
    table = normalize_sw_daily(
        [
            sw_row(trade_date="20260911", ts_code="801050.SI"),
            sw_row(trade_date="20260910", ts_code="801010.SI"),
        ]
    )
    assert table.get_column("date").to_list() == [date(2026, 9, 10), date(2026, 9, 11)]


def test_行业日线缺字段直接报错():
    broken = sw_row()
    del broken["total_mv"]
    with pytest.raises(NormalizeError, match="total_mv"):
        normalize_sw_daily([broken])


# ── 通用 ────────────────────────────────────────────────────────


def test_空输入都返回空表():
    assert normalize_index_classify([]).height == 0
    assert normalize_industry_member([]).height == 0
    assert normalize_sw_daily([]).height == 0


def test_类型固定():
    table = normalize_sw_daily([sw_row()])
    assert table.schema["date"] == pl.Date
    assert table.schema["amount"] == pl.Float64
    assert table.schema["code"] == pl.String

    members = normalize_industry_member([member_row()])
    assert members.schema["in_date"] == pl.Date
    assert members.schema["out_date"] == pl.Date
