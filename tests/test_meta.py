"""基础数据归一的测试：日期转换、空值语义、排序。小表格，不联网。"""

from __future__ import annotations

import logging
from datetime import date

import polars as pl
import pytest

from litmus.data.loaders.meta import (
    NormalizeError,
    normalize_namechange,
    normalize_stock_basic,
    normalize_trade_cal,
)


def basic_row(**overrides) -> dict:
    row = {
        "ts_code": "600519.SH",
        "symbol": "600519",
        "name": "贵州茅台",
        "area": "贵州",
        "industry": "白酒",
        "cnspell": "gzmt",
        "market": "主板",
        "list_status": "L",
        "list_date": "20010827",
        "delist_date": None,
    }
    return {**row, **overrides}


def cal_row(**overrides) -> dict:
    row = {
        "exchange": "SSE",
        "cal_date": "20260911",
        "is_open": "1",
        "pretrade_date": "20260910",
    }
    return {**row, **overrides}


def name_row(**overrides) -> dict:
    row = {
        "ts_code": "600848.SH",
        "name": "上海临港",
        "start_date": "20151118",
        "end_date": None,
        "ann_date": "20151117",
        "change_reason": "改名",
    }
    return {**row, **overrides}


# ── 股票列表 ────────────────────────────────────────────────────


def test_上市日期转成日期类型():
    row = normalize_stock_basic([basic_row()]).row(0, named=True)
    assert row["list_date"] == date(2001, 8, 27)


def test_在市股票没有退市日期():
    row = normalize_stock_basic([basic_row()]).row(0, named=True)
    assert row["delist_date"] is None


def test_退市股保留并带退市日期():
    row = normalize_stock_basic(
        [basic_row(ts_code="600001.SH", list_status="D", delist_date="20190424")]
    ).row(0, named=True)
    assert row["status"] == "D"
    assert row["delist_date"] == date(2019, 4, 24)


def test_退市日期是空串也算没有():
    """代理有时返回空串而不是 null。"""
    row = normalize_stock_basic([basic_row(delist_date="")]).row(0, named=True)
    assert row["delist_date"] is None


def test_拼音缩写改名成pinyin():
    row = normalize_stock_basic([basic_row()]).row(0, named=True)
    assert row["pinyin"] == "gzmt"
    assert "cnspell" not in row


def test_北交所照常保留():
    table = normalize_stock_basic([basic_row(ts_code="920992.BJ", market="北交所")])
    assert table.row(0, named=True)["code"] == "920992.BJ"


def test_按代码排序():
    table = normalize_stock_basic([basic_row(ts_code="600519.SH"), basic_row(ts_code="000001.SZ")])
    assert table.get_column("code").to_list() == ["000001.SZ", "600519.SH"]


def test_缺上市日期不中断只是记一笔(caplog):
    """缺上市日期只影响这一只的次新股判定，不该让整次同步失败。"""
    with caplog.at_level(logging.WARNING):
        table = normalize_stock_basic([basic_row(list_date=None)])

    assert table.row(0, named=True)["list_date"] is None
    assert "次新股" in caplog.text


def test_股票列表缺字段直接报错():
    broken = basic_row()
    del broken["list_status"]
    with pytest.raises(NormalizeError, match="list_status"):
        normalize_stock_basic([broken])


# ── 交易日历 ────────────────────────────────────────────────────


def test_交易日is_open为真():
    assert normalize_trade_cal([cal_row()]).row(0, named=True)["is_open"] is True


def test_休市日也保留():
    table = normalize_trade_cal([cal_row(cal_date="20260912", is_open="0")])
    row = table.row(0, named=True)
    assert row["is_open"] is False
    assert row["date"] == date(2026, 9, 12)


def test_is_open是数字也认():
    """代理可能把 "1" 变成数字 1。"""
    assert normalize_trade_cal([cal_row(is_open=1)]).row(0, named=True)["is_open"] is True


def test_上一个交易日为空时是空值():
    row = normalize_trade_cal([cal_row(pretrade_date="")]).row(0, named=True)
    assert row["pretrade_date"] is None


def test_日历按日期排序():
    table = normalize_trade_cal([cal_row(cal_date="20260911"), cal_row(cal_date="20260901")])
    assert table.get_column("date").to_list() == [date(2026, 9, 1), date(2026, 9, 11)]


# ── 曾用名 ──────────────────────────────────────────────────────


def test_现用名的结束日期为空():
    row = normalize_namechange([name_row()]).row(0, named=True)
    assert row["end_date"] is None
    assert row["name"] == "上海临港"


def test_旧名带起止日期():
    row = normalize_namechange(
        [name_row(name="ST自仪", start_date="20061026", end_date="20070513")]
    ).row(0, named=True)
    assert row["start_date"] == date(2006, 10, 26)
    assert row["end_date"] == date(2007, 5, 13)


def test_曾用名按代码和开始日期排序():
    table = normalize_namechange(
        [
            name_row(name="上海临港", start_date="20151118"),
            name_row(name="自仪股份", start_date="20070514"),
            name_row(ts_code="000001.SZ", name="深发展A", start_date="19910403"),
        ]
    )
    assert table.get_column("name").to_list() == ["深发展A", "自仪股份", "上海临港"]


def test_曾用名缺字段直接报错():
    broken = name_row()
    del broken["change_reason"]
    with pytest.raises(NormalizeError, match="change_reason"):
        normalize_namechange([broken])


def test_空输入返回空表而不是报错():
    assert normalize_stock_basic([]).height == 0
    assert normalize_trade_cal([]).height == 0
    assert normalize_namechange([]).is_empty()


def test_归一后的类型固定():
    table = normalize_stock_basic([basic_row()])
    assert table.schema["list_date"] == pl.Date
    assert table.schema["code"] == pl.String
