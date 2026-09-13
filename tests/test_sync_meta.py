"""基础数据同步的测试：三种上市状态都拉、落到 meta/ 下、记账。用假 client，不联网。"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import (
    NAMECHANGE_TABLE,
    STOCK_BASIC_TABLE,
    TRADE_CAL_TABLE,
    DataSync,
    SyncError,
)

LISTED = {
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
DELISTED = {**LISTED, "ts_code": "600001.SH", "name": "邯郸钢铁", "list_status": "D"}
DELISTED["delist_date"] = "20190424"
SUSPENDED = {**LISTED, "ts_code": "600002.SH", "name": "齐鲁石化", "list_status": "P"}

CALENDAR = [
    {"exchange": "SSE", "cal_date": "20260911", "is_open": "1", "pretrade_date": "20260910"},
    {"exchange": "SSE", "cal_date": "20260912", "is_open": "0", "pretrade_date": "20260911"},
]
NAMES = [
    {
        "ts_code": "600848.SH",
        "name": "上海临港",
        "start_date": "20151118",
        "end_date": None,
        "ann_date": "20151117",
        "change_reason": "改名",
    }
]


class FakeClient:
    """按上市状态分发股票列表，同时记录每次调用的参数与 fields。"""

    def __init__(self, *, no_stocks: bool = False):
        self.no_stocks = no_stocks
        self.calls: list[tuple[str, dict, str | None]] = []

    def call(self, api_name: str, params: dict | None = None, fields: str | None = None):
        self.calls.append((api_name, dict(params or {}), fields))
        if api_name == "stock_basic":
            if self.no_stocks:
                return []
            by_status = {"L": [LISTED], "D": [DELISTED], "P": [SUSPENDED]}
            return by_status[(params or {})["list_status"]]
        if api_name == "trade_cal":
            return CALENDAR
        if api_name == "namechange":
            return NAMES
        raise AssertionError(f"测试没准备 {api_name}")

    def params_for(self, api_name: str) -> list[dict]:
        return [p for name, p, _ in self.calls if name == api_name]

    def fields_for(self, api_name: str) -> str | None:
        return next(f for name, _, f in self.calls if name == api_name)


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path)


def run(store: MarketStore, client: FakeClient):
    manifest = Manifest.load(store)
    written = DataSync(client, store).sync_meta("20160101", "20260930", manifest)
    return written, manifest


# ── 拉取方式 ────────────────────────────────────────────────────


def test_三种上市状态各拉一次(store):
    """只拉 L 会把退市股漏掉，历史回测就成了幸存者偏差。"""
    client = FakeClient()
    run(store, client)

    statuses = [p["list_status"] for p in client.params_for("stock_basic")]
    assert sorted(statuses) == ["D", "L", "P"]


def test_默认不返回的字段要显式请求(store):
    """list_status 和 delist_date 在接口里是默认不显示的，不写 fields 就拿不到。"""
    client = FakeClient()
    run(store, client)

    requested = client.fields_for("stock_basic")
    assert "list_status" in requested
    assert "delist_date" in requested


def test_交易日历按传入的区间拉(store):
    client = FakeClient()
    run(store, client)

    params = client.params_for("trade_cal")[0]
    assert params["start_date"] == "20160101"
    assert params["end_date"] == "20260930"


# ── 落盘与记账 ──────────────────────────────────────────────────


def test_三张表都落到meta目录下(store):
    run(store, FakeClient())

    for table in (STOCK_BASIC_TABLE, TRADE_CAL_TABLE, NAMECHANGE_TABLE):
        assert store.has_table(table), f"{table} 没落盘"
        assert store.table_path(table).is_relative_to(store.market / "meta")


def test_退市和暂停上市的股票都在表里(store):
    run(store, FakeClient())

    table = store.read_table(STOCK_BASIC_TABLE)
    assert sorted(table.get_column("status").to_list()) == ["D", "L", "P"]
    delisted = table.filter(pl.col("status") == "D").row(0, named=True)
    assert delisted["delist_date"] is not None


def test_返回的行数与落盘一致(store):
    written, _ = run(store, FakeClient())

    assert written[STOCK_BASIC_TABLE] == 3
    assert written[TRADE_CAL_TABLE] == 2
    assert store.read_table(TRADE_CAL_TABLE).height == 2


def test_记账里有行数和备注(store):
    _, manifest = run(store, FakeClient())

    record = manifest.tables[STOCK_BASIC_TABLE]
    assert record.rows == 3
    assert "退市" in record.note
    assert manifest.tables[TRADE_CAL_TABLE].note == "20160101~20260930"


def test_记账已经存盘(store):
    run(store, FakeClient())

    reloaded = Manifest.load(store)
    assert reloaded.tables[NAMECHANGE_TABLE].rows == 1


# ── 拉空了不能覆盖 ──────────────────────────────────────────────


def test_一行都没拉到时不覆盖已有的表(store):
    """接口临时抽风返回空，不能把本地好好的股票列表清掉。"""
    run(store, FakeClient())
    before = store.read_table(STOCK_BASIC_TABLE).height

    with pytest.raises(SyncError, match="不覆盖"):
        run(store, FakeClient(no_stocks=True))

    assert store.read_table(STOCK_BASIC_TABLE).height == before
