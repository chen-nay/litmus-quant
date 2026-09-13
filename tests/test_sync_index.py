"""指数同步的测试：日线一次到位、成分按月拉、两张表落在 index/ 下。不联网。"""

from __future__ import annotations

from pathlib import Path

import pytest

from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import (
    BENCHMARK_INDEXES,
    INDEX_DAILY_TABLE,
    INDEX_WEIGHT_TABLE,
    DataSync,
    SyncError,
    month_ranges,
)

START, END = "20240101", "20240315"


def daily_rows(ts_code: str) -> list[dict]:
    return [
        {
            "ts_code": ts_code,
            "trade_date": day,
            "open": 3320.6898,
            "high": 3325.6070,
            "low": 3291.7842,
            "close": 3321.8248,
            "pct_chg": 0.38,
            "vol": 123456.0,
            "amount": 987654.0,
        }
        for day in ("20240104", "20240105")
    ]


def weight_rows(index_code: str, start_date: str) -> list[dict]:
    """每个月一个快照，成分随月份不同——好验证「所有快照都留下来了」。"""
    return [
        {
            "index_code": index_code,
            "con_code": con,
            "trade_date": start_date,
            "weight": 0.8656,
        }
        for con in ("000001.SZ", "600519.SH")
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
        if api_name == "index_daily":
            return daily_rows(params["ts_code"])
        if api_name == "index_weight":
            return weight_rows(params["index_code"], params["start_date"])
        raise AssertionError(f"测试没准备 {api_name}")

    def params_for(self, api_name: str) -> list[dict]:
        return [params for name, params, _ in self.calls if name == api_name]


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path)


def run(store: MarketStore, client: FakeClient):
    manifest = Manifest.load(store)
    written = DataSync(client, store, workers=4).sync_index(START, END, manifest)
    return written, manifest


# ── 自然月区间 ──────────────────────────────────────────────────


def test_一年十二个月():
    assert len(month_ranges("20240101", "20241231")) == 12


def test_首月按起点裁剪():
    first = month_ranges("20240115", "20240331")[0]
    assert first == ("20240115", "20240131")


def test_末月按终点裁剪():
    last = month_ranges("20240101", "20240315")[-1]
    assert last == ("20240301", "20240315")


def test_闰年二月是二十九天():
    february = month_ranges("20240201", "20240229")[0]
    assert february == ("20240201", "20240229")


def test_平年二月是二十八天():
    february = month_ranges("20230201", "20230228")[0]
    assert february == ("20230201", "20230228")


def test_不足一个月也有一个区间():
    assert month_ranges("20240110", "20240115") == [("20240110", "20240115")]


# ── 拉取方式 ────────────────────────────────────────────────────


def test_日线一个指数一次调用覆盖整个区间(store):
    """index_daily 单次 8000 行，十年才 2600 个交易日，不必按天或按年循环。"""
    client = FakeClient()
    run(store, client)

    windows = client.params_for("index_daily")
    assert len(windows) == len(BENCHMARK_INDEXES)
    for params in windows:
        assert params["start_date"] == START
        assert params["end_date"] == END


def test_成分按自然月拉(store):
    """按月拉，一个月正好一页装得下，彻底不需要翻页。"""
    client = FakeClient()
    run(store, client)

    windows = client.params_for("index_weight")
    assert len(windows) == len(BENCHMARK_INDEXES) * len(month_ranges(START, END))
    assert {(w["start_date"], w["end_date"]) for w in windows} == set(month_ranges(START, END))


def test_两个宽基指数都拉(store):
    client = FakeClient()
    run(store, client)

    assert {p["ts_code"] for p in client.params_for("index_daily")} == set(BENCHMARK_INDEXES)
    assert {p["index_code"] for p in client.params_for("index_weight")} == set(BENCHMARK_INDEXES)


# ── 落盘与记账 ──────────────────────────────────────────────────


def test_两张表落在index目录下(store):
    run(store, FakeClient())

    for table in (INDEX_DAILY_TABLE, INDEX_WEIGHT_TABLE):
        assert store.has_table(table)
        assert store.table_path(table).is_relative_to(store.market / "index")


def test_所有月份的快照都留下来(store):
    """只留最新一期就没法还原当时的股票池了。"""
    run(store, FakeClient())

    weights = store.read_table(INDEX_WEIGHT_TABLE)
    assert weights.get_column("date").n_unique() == len(month_ranges(START, END))


def test_记账里有备注(store):
    _, manifest = run(store, FakeClient())

    assert "000300.SH" in manifest.tables[INDEX_DAILY_TABLE].note
    assert manifest.tables[INDEX_WEIGHT_TABLE].note == "月度快照"


def test_拉空了不覆盖已有的表(store):
    run(store, FakeClient())
    before = store.read_table(INDEX_DAILY_TABLE).height

    with pytest.raises(SyncError, match="不覆盖"):
        run(store, FakeClient(empty={"index_daily"}))

    assert store.read_table(INDEX_DAILY_TABLE).height == before
