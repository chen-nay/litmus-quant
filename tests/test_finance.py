"""财务与事件归一的测试：改名、日期、空值、更正记录必须保留。小表格，不联网。"""

from __future__ import annotations

import logging
from datetime import date

import polars as pl
import pytest

from litmus.data.loaders.finance import (
    NormalizeError,
    normalize_disclosure,
    normalize_fina_indicator,
    normalize_forecast,
    normalize_share_float,
)


def fina_row(**overrides) -> dict:
    row = {
        "ts_code": "600519.SH",
        "ann_date": "20260428",
        "end_date": "20260331",
        "roe": 12.5,
        "roe_yearly": 50.0,
        "or_yoy": 18.3,
        "netprofit_yoy": 22.1,
        "update_flag": "1",
    }
    return {**row, **overrides}


def forecast_row(**overrides) -> dict:
    row = {
        "ts_code": "600519.SH",
        "ann_date": "20260115",
        "end_date": "20251231",
        "type": "预增",
        "p_change_min": 30.0,
        "p_change_max": 50.0,
    }
    return {**row, **overrides}


def disclosure_row(**overrides) -> dict:
    row = {
        "ts_code": "600519.SH",
        "ann_date": "20260401",
        "end_date": "20251231",
        "pre_date": "20260425",
        "actual_date": "20260428",
    }
    return {**row, **overrides}


def float_row(**overrides) -> dict:
    row = {
        "ts_code": "600519.SH",
        "ann_date": "20251220",
        "float_date": "20260315",
        "float_share": 25076106.0,
        "float_ratio": 1.9041,
        "share_type": "定增股份",
    }
    return {**row, **overrides}


# ── 财务指标 ────────────────────────────────────────────────────


def test_报告期改名成period():
    row = normalize_fina_indicator([fina_row()]).row(0, named=True)
    assert row["period"] == date(2026, 3, 31)
    assert row["ann_date"] == date(2026, 4, 28)


def test_同比字段改成业务名字():
    row = normalize_fina_indicator([fina_row()]).row(0, named=True)
    assert row["revenue_yoy"] == 18.3
    assert row["profit_yoy"] == 22.1
    assert "or_yoy" not in row


def test_两种roe都保留():
    """roe 与 roe_yearly 口径不同，先都存着，等实测确定用哪个。"""
    row = normalize_fina_indicator([fina_row()]).row(0, named=True)
    assert row["roe"] == 12.5
    assert row["roe_yearly"] == 50.0


def test_更正公告必须保留而不是去重():
    """同一报告期的更正记录 ann_date 不同，是两条不同的记录；
    哪条有效由读取时的 PIT 规则决定，不能在这里替它做主。"""
    table = normalize_fina_indicator(
        [
            fina_row(ann_date="20260428", netprofit_yoy=22.1),
            fina_row(ann_date="20260830", netprofit_yoy=19.4),  # 更正
        ]
    )
    assert table.height == 2
    assert table.get_column("profit_yoy").to_list() == [22.1, 19.4]


def test_整行重复才去掉():
    table = normalize_fina_indicator([fina_row(), fina_row(), fina_row()])
    assert table.height == 1


def test_同一公告日的几个版本都保留并带上更新标志():
    """实测同一 (股票, 报告期, 公告日) 有几千组数值不同的行，只有 update_flag 能区分。
    归一层原样保留，哪条有效由读取时决定。"""
    table = normalize_fina_indicator(
        [
            fina_row(roe_yearly=6.4353, update_flag="0"),
            fina_row(roe_yearly=6.4077, update_flag="1"),
        ]
    )
    assert table.height == 2
    assert sorted(table.get_column("update_flag").to_list()) == ["0", "1"]


def test_缺公告日不丢数据只记一笔(caplog):
    with caplog.at_level(logging.WARNING):
        table = normalize_fina_indicator([fina_row(ann_date=None)])

    assert table.height == 1
    assert table.row(0, named=True)["ann_date"] is None
    assert "PIT" in caplog.text


def test_财务指标缺字段直接报错():
    broken = fina_row()
    del broken["netprofit_yoy"]
    with pytest.raises(NormalizeError, match="netprofit_yoy"):
        normalize_fina_indicator([broken])


def test_按代码报告期公告日排序():
    table = normalize_fina_indicator(
        [
            fina_row(ts_code="600519.SH", end_date="20260630"),
            fina_row(ts_code="000001.SZ", end_date="20260331"),
            fina_row(ts_code="600519.SH", end_date="20260331"),
        ]
    )
    assert table.get_column("code").to_list() == ["000001.SZ", "600519.SH", "600519.SH"]
    assert table.get_column("period").to_list()[1:] == [date(2026, 3, 31), date(2026, 6, 30)]


# ── 业绩预告 ────────────────────────────────────────────────────


def test_预告类型改名避免和python的type撞车():
    row = normalize_forecast([forecast_row()]).row(0, named=True)
    assert row["forecast_type"] == "预增"
    assert row["p_change_min"] == 30.0


def test_预告修订保留两条():
    table = normalize_forecast(
        [forecast_row(ann_date="20260115"), forecast_row(ann_date="20260125", p_change_min=10.0)]
    )
    assert table.height == 2


def test_预告缺字段直接报错():
    broken = forecast_row()
    del broken["p_change_max"]
    with pytest.raises(NormalizeError, match="p_change_max"):
        normalize_forecast([broken])


# ── 财报披露计划 ────────────────────────────────────────────────


def test_实际披露日和预计披露日都留着():
    """事件日必须用 actual_date；pre_date 会变，用它等于用了当时还不知道的信息。"""
    row = normalize_disclosure([disclosure_row()]).row(0, named=True)
    assert row["actual_date"] == date(2026, 4, 28)
    assert row["pre_date"] == date(2026, 4, 25)


def test_还没披露时实际披露日为空():
    row = normalize_disclosure([disclosure_row(actual_date=None)]).row(0, named=True)
    assert row["actual_date"] is None
    assert row["pre_date"] == date(2026, 4, 25)


def test_披露计划空串也算没有():
    row = normalize_disclosure([disclosure_row(actual_date="")]).row(0, named=True)
    assert row["actual_date"] is None


# ── 限售解禁 ────────────────────────────────────────────────────


def test_解禁日和公告日是两个日期():
    """公告日在前、解禁日在后，事件发生在解禁日。"""
    row = normalize_share_float([float_row()]).row(0, named=True)
    assert row["ann_date"] == date(2025, 12, 20)
    assert row["float_date"] == date(2026, 3, 15)


def test_同一个解禁日多个股东各占一行():
    table = normalize_share_float(
        [
            float_row(float_share=25076106.0, share_type="定增股份"),
            float_row(float_share=11265340.0, share_type="首发原股东限售股份"),
        ]
    )
    assert table.height == 2
    assert table.get_column("float_share").sum() == pytest.approx(36341446.0)


def test_解禁股数保持股不做换算():
    row = normalize_share_float([float_row()]).row(0, named=True)
    assert row["float_share"] == 25076106.0
    assert row["float_ratio"] == pytest.approx(1.9041)


def test_解禁缺字段直接报错():
    broken = float_row()
    del broken["float_date"]
    with pytest.raises(NormalizeError, match="float_date"):
        normalize_share_float([broken])


# ── 通用 ────────────────────────────────────────────────────────


def test_空输入都返回空表():
    assert normalize_fina_indicator([]).height == 0
    assert normalize_forecast([]).height == 0
    assert normalize_disclosure([]).height == 0
    assert normalize_share_float([]).height == 0


def test_类型固定():
    table = normalize_fina_indicator([fina_row()])
    assert table.schema["period"] == pl.Date
    assert table.schema["roe"] == pl.Float64
    assert table.schema["code"] == pl.String
