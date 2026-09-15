"""指数同步的测试：日线一次到位、成分按整月拉并按月记账、再次同步只补没走完的月份。不联网。"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import (
    BENCHMARK_INDEXES,
    INDEX_DAILY_TABLE,
    INDEX_WEIGHT_DATASET,
    DataSync,
    SyncError,
    month_ranges,
    month_window,
)

START, END = "20240101", "20240315"
MONTHS = ("2024-01", "2024-02", "2024-03")


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
    def __init__(
        self, *, empty: set[str] | None = None, missing: set[tuple[str, str]] | None = None
    ):
        self.empty = empty or set()
        self.missing = missing or set()  # (指数, 月份)：这个月还没有快照
        self.calls: list[tuple[str, dict, str | None]] = []

    def call(self, api_name: str, params: dict | None = None, fields: str | None = None):
        params = dict(params or {})
        self.calls.append((api_name, params, fields))
        if api_name in self.empty:
            return []
        if api_name == "index_daily":
            return daily_rows(params["ts_code"])
        if api_name == "index_weight":
            month = f"{params['start_date'][:4]}-{params['start_date'][4:6]}"
            if (params["index_code"], month) in self.missing:
                return []
            return weight_rows(params["index_code"], params["start_date"])
        raise AssertionError(f"测试没准备 {api_name}")

    def params_for(self, api_name: str) -> list[dict]:
        return [params for name, params, _ in self.calls if name == api_name]

    def weight_months(self) -> set[str]:
        return {
            f"{p['start_date'][:4]}-{p['start_date'][4:6]}" for p in self.params_for("index_weight")
        }


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


def test_整月窗口():
    assert month_window("2024-02") == ("20240201", "20240229")
    assert month_window("2023-12") == ("20231201", "20231231")


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


def test_成分按整月拉_不按区间裁剪(store):
    """END 是 3 月 15 日，3 月也拉整月：月文件要么不存在，要么是整月。"""
    client = FakeClient()
    run(store, client)

    windows = client.params_for("index_weight")
    assert len(windows) == len(BENCHMARK_INDEXES) * len(MONTHS)
    assert {(w["start_date"], w["end_date"]) for w in windows} == {month_window(m) for m in MONTHS}


def test_两个宽基指数都拉(store):
    client = FakeClient()
    run(store, client)

    assert {p["ts_code"] for p in client.params_for("index_daily")} == set(BENCHMARK_INDEXES)
    assert {p["index_code"] for p in client.params_for("index_weight")} == set(BENCHMARK_INDEXES)


# ── 落盘与记账 ──────────────────────────────────────────────────


def test_日线是整表_成分按月落盘(store):
    run(store, FakeClient())

    assert store.table_path(INDEX_DAILY_TABLE).is_relative_to(store.market / "index")
    assert store.dataset_dir(INDEX_WEIGHT_DATASET).is_relative_to(store.market / "index")
    assert store.months(INDEX_WEIGHT_DATASET) == MONTHS


def test_所有月份的快照都留下来(store):
    """只留最新一期就没法还原当时的股票池了。"""
    run(store, FakeClient())

    weights = pl.concat(store.read_month(INDEX_WEIGHT_DATASET, m) for m in MONTHS)
    assert weights.get_column("date").n_unique() == len(MONTHS)
    assert set(weights.get_column("index_code")) == set(BENCHMARK_INDEXES)


def test_最近两个月记为没走完(store):
    """发布有滞后：8/31 的快照 9/13 才看得到，月末那期也可能下个月才发。"""
    _, manifest = run(store, FakeClient())

    assert manifest.month(INDEX_WEIGHT_DATASET, "2024-01").complete
    assert not manifest.month(INDEX_WEIGHT_DATASET, "2024-02").complete
    assert not manifest.month(INDEX_WEIGHT_DATASET, "2024-03").complete


def test_记账里有备注(store):
    _, manifest = run(store, FakeClient())

    assert "000300.SH" in manifest.tables[INDEX_DAILY_TABLE].note


# ── 增量同步 ────────────────────────────────────────────────────


def test_再次同步只拉没走完的月份(store):
    run(store, FakeClient())
    client = FakeClient()
    run(store, client)

    assert client.weight_months() == {"2024-02", "2024-03"}


def test_月文件被删了会补回来(store):
    run(store, FakeClient())
    store.month_path(INDEX_WEIGHT_DATASET, "2024-01").unlink()
    client = FakeClient()
    run(store, client)

    assert "2024-01" in client.weight_months()
    assert store.has_month(INDEX_WEIGHT_DATASET, "2024-01")


def test_本月还没发布就不写文件也不记账(store):
    missing = {(code, "2024-03") for code in BENCHMARK_INDEXES}
    _, manifest = run(store, FakeClient(missing=missing))

    assert not store.has_month(INDEX_WEIGHT_DATASET, "2024-03")
    assert manifest.month(INDEX_WEIGHT_DATASET, "2024-03") is None
    assert store.has_month(INDEX_WEIGHT_DATASET, "2024-02")


def test_早就该有快照的月份空了直接报错(store):
    """实测 2016 年以来每个月两个指数都有快照；早的月份空了说明数据源出了问题，一个月都不写。"""
    with pytest.raises(SyncError, match="2024-01"):
        run(store, FakeClient(missing={("000905.SH", "2024-01")}))

    assert store.months(INDEX_WEIGHT_DATASET) == ()


def test_拉空了不覆盖已有的表(store):
    run(store, FakeClient())
    before = store.read_table(INDEX_DAILY_TABLE).height

    with pytest.raises(SyncError, match="不覆盖"):
        run(store, FakeClient(empty={"index_daily"}))

    assert store.read_table(INDEX_DAILY_TABLE).height == before
