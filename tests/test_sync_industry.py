"""申万行业同步的测试：先清单后按行业、历史归属不能漏、三张表落盘。不联网。"""

from __future__ import annotations

from pathlib import Path

import pytest

from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import (
    SW_DAILY_TABLE,
    SW_INDUSTRY_TABLE,
    SW_MEMBER_TABLE,
    DataSync,
    SyncError,
)

START, END = "20160101", "20260913"
INDUSTRIES = [("801010.SI", "农林牧渔"), ("801050.SI", "有色金属")]


def classify_rows() -> list[dict]:
    rows = [
        {
            "index_code": code,
            "industry_name": name,
            "level": "L1",
            "parent_code": "0",
            "src": "SW2021",
        }
        for code, name in INDUSTRIES
    ]
    # 接口一次会把 L2 也返回，归一时要滤掉
    rows.append(
        {
            "index_code": "801011.SI",
            "industry_name": "种植业",
            "level": "L2",
            "parent_code": "801010.SI",
            "src": "SW2021",
        }
    )
    return rows


def member_rows(l1_code: str) -> list[dict]:
    name = dict(INDUSTRIES)[l1_code]
    return [
        {
            "l1_code": l1_code,
            "l1_name": name,
            "ts_code": "600547.SH",
            "in_date": "20030826",
            "out_date": None,
        },
        {
            "l1_code": l1_code,
            "l1_name": name,
            "ts_code": "000001.SZ",
            "in_date": "19910403",
            "out_date": "20220728",  # 已经调出，靠 is_new=N 才拿得到
        },
    ]


def sw_daily_rows(ts_code: str) -> list[dict]:
    return [
        {
            "ts_code": ts_code,
            "trade_date": day,
            "name": dict(INDUSTRIES)[ts_code],
            "open": 2986.75,
            "high": 3000.0,
            "low": 2940.0,
            "close": 2946.60,
            "pct_change": -1.34,
            "amount": 83532.0,
            "pb": 2.66,
            "float_mv": 500000.0,
            "total_mv": 800000.0,
        }
        for day in ("20260910", "20260911")
    ]


class FakeClient:
    def __init__(self, *, empty: set[str] | None = None):
        self.empty = empty or set()
        self.calls: list[tuple[str, dict, str | None]] = []

    def call(self, api_name: str, params: dict | None = None, fields: str | None = None):
        params = dict(params or {})
        self.calls.append((api_name, params, fields))
        if api_name in self.empty:
            return []
        if api_name == "index_classify":
            return classify_rows()
        if api_name == "index_member_all":
            return member_rows(params["l1_code"])
        if api_name == "sw_daily":
            return sw_daily_rows(params["ts_code"])
        raise AssertionError(f"测试没准备 {api_name}")

    def params_for(self, api_name: str) -> list[dict]:
        return [params for name, params, _ in self.calls if name == api_name]


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path)


def run(store: MarketStore, client: FakeClient):
    manifest = Manifest.load(store)
    written = DataSync(client, store, workers=4).sync_industry(START, END, manifest)
    return written, manifest


# ── 拉取方式 ────────────────────────────────────────────────────


def test_先拉清单再按行业拉(store):
    client = FakeClient()
    run(store, client)

    assert len(client.params_for("index_classify")) == 1
    assert len(client.params_for("index_member_all")) == len(INDUSTRIES)
    assert len(client.params_for("sw_daily")) == len(INDUSTRIES)


def test_清单只问一级行业(store):
    client = FakeClient()
    run(store, client)

    params = client.params_for("index_classify")[0]
    assert params["level"] == "L1"
    assert params["src"] == "SW2021"


def test_历史归属必须传is_new等于N(store):
    """接口默认只给当前成分。漏了这个参数，换过行业的公司会被算进今天的行业，
    十年回测里每只股票都用今天的归属——这是最隐蔽的一类幸存者偏差。"""
    client = FakeClient()
    run(store, client)

    for params in client.params_for("index_member_all"):
        assert params["is_new"] == "N"


def test_行业日线一次覆盖整个区间(store):
    """sw_daily 单次 4000 行，十年才 2600 个交易日，一个行业一次调用就够。"""
    client = FakeClient()
    run(store, client)

    for params in client.params_for("sw_daily"):
        assert params["start_date"] == START
        assert params["end_date"] == END


def test_按清单里的行业代码逐个拉(store):
    client = FakeClient()
    run(store, client)

    pulled = sorted(p["l1_code"] for p in client.params_for("index_member_all"))
    assert pulled == sorted(code for code, _ in INDUSTRIES)


# ── 落盘与记账 ──────────────────────────────────────────────────


def test_三张表落在各自的位置(store):
    run(store, FakeClient())

    assert store.table_path(SW_INDUSTRY_TABLE).is_relative_to(store.market / "meta")
    assert store.table_path(SW_MEMBER_TABLE).is_relative_to(store.market / "meta")
    assert store.table_path(SW_DAILY_TABLE).is_relative_to(store.market / "board")


def test_清单里只剩一级行业(store):
    written, _ = run(store, FakeClient())

    assert written[SW_INDUSTRY_TABLE] == len(INDUSTRIES)  # L2 那行被滤掉了
    assert store.read_table(SW_INDUSTRY_TABLE).height == len(INDUSTRIES)


def test_已调出的成分也落盘(store):
    run(store, FakeClient())

    members = store.read_table(SW_MEMBER_TABLE)
    assert members.filter(members["out_date"].is_not_null()).height > 0


def test_行业日线不按月分片(store):
    """31 个行业十年才 8 万行，整张存就够，不值得引入按月分片那套复杂度。"""
    run(store, FakeClient())

    assert store.months("board") == ()
    assert store.has_table(SW_DAILY_TABLE)


def test_记账里有备注(store):
    _, manifest = run(store, FakeClient())

    assert "SW2021" in manifest.tables[SW_INDUSTRY_TABLE].note
    assert "is_new=N" in manifest.tables[SW_MEMBER_TABLE].note
    assert manifest.tables[SW_DAILY_TABLE].note == f"{START}~{END}"


# ── 拉空了就停下 ────────────────────────────────────────────────


def test_行业清单为空直接报错(store):
    """清单是后面两步的输入，空了就什么都拉不到，要在这里停住。"""
    client = FakeClient(empty={"index_classify"})

    with pytest.raises(SyncError, match="行业清单"):
        run(store, client)


def test_行业日线为空不覆盖已有的表(store):
    run(store, FakeClient())
    before = store.read_table(SW_DAILY_TABLE).height

    with pytest.raises(SyncError, match="不覆盖"):
        run(store, FakeClient(empty={"sw_daily"}))

    assert store.read_table(SW_DAILY_TABLE).height == before
