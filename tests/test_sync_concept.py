"""概念板块同步的测试：先定快照日、只拉概念板块、成分按板块拉、三张表落盘。不联网。"""

from __future__ import annotations

from pathlib import Path

import pytest

from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import (
    SNAPSHOT_LOOKBACK_DAYS,
    TDX_CONCEPT_TABLE,
    TDX_DAILY_TABLE,
    TDX_MEMBER_TABLE,
    DataSync,
    SyncError,
)

START, END = "20160101", "20260913"  # 9/12、9/13 是周末
#: 交易日要比 SNAPSHOT_LOOKBACK_DAYS 多，才测得出「最多往回试几天」
TRADING_DAYS = (
    "20260903",
    "20260904",
    "20260907",
    "20260908",
    "20260909",
    "20260910",
    "20260911",
)
LATEST = TRADING_DAYS[-1]
CONCEPTS = (("880728.TDX", "航运概念"), ("880528.TDX", "军工信息化"))
#: 接口会把行业、风格、地区板块和概念板块一起返回，归一时要滤掉
OTHER_BOARDS = (("880355.TDX", "日用化工", "行业板块"), ("880868.TDX", "高贝塔值", "风格板块"))


def index_rows(day: str, concepts) -> list[dict]:
    rows = [
        {"ts_code": code, "trade_date": day, "name": name, "idx_type": "概念板块", "idx_count": 2}
        for code, name in concepts
    ]
    rows += [
        {"ts_code": code, "trade_date": day, "name": name, "idx_type": idx_type, "idx_count": 20}
        for code, name, idx_type in OTHER_BOARDS
    ]
    return rows


def member_rows(ts_code: str, day: str) -> list[dict]:
    return [
        {"ts_code": ts_code, "trade_date": day, "con_code": code, "con_name": name}
        for code, name in (("000039.SZ", "中集集团"), ("833171.BJ", "国航远洋"))
    ]


def daily_rows(ts_code: str) -> list[dict]:
    return [
        {
            "ts_code": ts_code,
            "trade_date": day,
            "open": 1234.5,
            "high": 1250.0,
            "low": 1220.0,
            "close": 1245.6,
            "pct_change": 1.23,
            "amount": 56789.0,
            "turnover_rate": 2.5,
            "up_num": 40,
            "limit_up_num": 3,
            "pb": "2.66",
            "float_mv": 1234.0,
        }
        for day in ("20260910", "20260911")
    ]


class FakeClient:
    """照真接口的语义返回：清单只在已发布的交易日才有，成分必须带板块代码。"""

    def __init__(self, *, published=TRADING_DAYS, concepts=CONCEPTS, empty: set[str] | None = None):
        self.published = set(published)
        self.concepts = concepts
        self.empty = empty or set()
        self.calls: list[tuple[str, dict, str | None]] = []

    def call(self, api_name: str, params: dict | None = None, fields: str | None = None):
        params = dict(params or {})
        self.calls.append((api_name, params, fields))
        if api_name in self.empty:
            return []
        if api_name == "trade_cal":
            return [
                {"cal_date": day}
                for day in TRADING_DAYS
                if params["start_date"] <= day <= params["end_date"]
            ]
        if api_name == "tdx_index":
            day = params["trade_date"]
            return index_rows(day, self.concepts) if day in self.published else []
        if api_name == "tdx_member":
            # 真接口不带板块代码时，一天全部板块约 8.4 万行、28 页，超过翻页上限
            assert "ts_code" in params, "成分必须按板块拉"
            return member_rows(params["ts_code"], params["trade_date"])
        if api_name == "tdx_daily":
            return daily_rows(params["ts_code"])
        raise AssertionError(f"测试没准备 {api_name}")

    def params_for(self, api_name: str) -> list[dict]:
        return [params for name, params, _ in self.calls if name == api_name]


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path)


def run(store: MarketStore, client: FakeClient):
    manifest = Manifest.load(store)
    written = DataSync(client, store, workers=4).sync_concept(START, END, manifest)
    return written, manifest


CONCEPT_CODES = {code for code, _ in CONCEPTS}


# ── 快照日 ──────────────────────────────────────────────────────


def test_快照日取end之前最近的交易日(store):
    """清单必须带交易日，非交易日返回 0 行。END 是周日，应该落到周五。"""
    client = FakeClient()
    run(store, client)

    assert client.params_for("tdx_index") == [{"trade_date": LATEST}]


def test_当天清单还没发布就往前退一天(store):
    """收盘前同步时，当天的清单还没有。"""
    client = FakeClient(published=TRADING_DAYS[:-1])
    written, manifest = run(store, client)

    assert [p["trade_date"] for p in client.params_for("tdx_index")] == [LATEST, "20260910"]
    assert {p["trade_date"] for p in client.params_for("tdx_member")} == {"20260910"}
    assert "20260910" in manifest.tables[TDX_CONCEPT_TABLE].note


def test_连续几个交易日都没有清单直接报错(store):
    """退几天还是空，就不是「还没发布」了，要停下来，而且成分和日线一个都不拉。"""
    client = FakeClient(published=())

    with pytest.raises(SyncError, match="板块清单"):
        run(store, client)

    tried = [p["trade_date"] for p in client.params_for("tdx_index")]
    assert tried == sorted(TRADING_DAYS, reverse=True)[:SNAPSHOT_LOOKBACK_DAYS]
    assert client.params_for("tdx_member") == []
    assert client.params_for("tdx_daily") == []


# ── 拉取方式 ────────────────────────────────────────────────────


def test_每个概念板块拉一次成分和一次日线(store):
    client = FakeClient()
    run(store, client)

    assert [p["ts_code"] for p in client.params_for("tdx_member")].count("880728.TDX") == 1
    assert len(client.params_for("tdx_member")) == len(CONCEPTS)
    assert len(client.params_for("tdx_daily")) == len(CONCEPTS)


def test_只拉概念板块(store):
    """行业、风格、地区板块混进来，板块排行就会把「高贝塔值」当概念讲。"""
    client = FakeClient()
    run(store, client)

    assert {p["ts_code"] for p in client.params_for("tdx_member")} == CONCEPT_CODES
    assert {p["ts_code"] for p in client.params_for("tdx_daily")} == CONCEPT_CODES


def test_成分取快照日那天(store):
    client = FakeClient()
    run(store, client)

    assert {p["trade_date"] for p in client.params_for("tdx_member")} == {LATEST}


def test_板块日线一次覆盖整个区间(store):
    client = FakeClient()
    run(store, client)

    for params in client.params_for("tdx_daily"):
        assert params["start_date"] == START
        assert params["end_date"] == END


# ── 落盘与记账 ──────────────────────────────────────────────────


def test_三张表落在各自的位置(store):
    run(store, FakeClient())

    assert store.table_path(TDX_CONCEPT_TABLE).is_relative_to(store.market / "meta")
    assert store.table_path(TDX_MEMBER_TABLE).is_relative_to(store.market / "meta")
    assert store.table_path(TDX_DAILY_TABLE).is_relative_to(store.market / "board")


def test_清单里只剩概念板块(store):
    written, _ = run(store, FakeClient())

    assert written[TDX_CONCEPT_TABLE] == len(CONCEPTS)
    assert set(store.read_table(TDX_CONCEPT_TABLE).get_column("code")) == CONCEPT_CODES


def test_成分和日线都落盘(store):
    written, _ = run(store, FakeClient())

    assert written[TDX_MEMBER_TABLE] == len(CONCEPTS) * 2
    assert written[TDX_DAILY_TABLE] == len(CONCEPTS) * 2


def test_记账写明快照日和实际区间(store):
    """成分是当前快照，这一点要在记账里看得出来；日线记实际区间，不记请求区间。"""
    _, manifest = run(store, FakeClient())

    assert LATEST in manifest.tables[TDX_CONCEPT_TABLE].note
    assert "当前成分" in manifest.tables[TDX_MEMBER_TABLE].note
    assert manifest.tables[TDX_DAILY_TABLE].note == "2026-09-10~2026-09-11"


# ── 拉空了就停下 ────────────────────────────────────────────────


def test_清单里没有概念板块直接报错(store):
    client = FakeClient(concepts=())

    with pytest.raises(SyncError, match="没有概念板块"):
        run(store, client)

    assert not store.has_table(TDX_CONCEPT_TABLE)


def test_板块日线为空不覆盖已有的表(store):
    run(store, FakeClient())
    before = store.read_table(TDX_DAILY_TABLE).height

    with pytest.raises(SyncError, match="不覆盖"):
        run(store, FakeClient(empty={"tdx_daily"}))

    assert store.read_table(TDX_DAILY_TABLE).height == before
