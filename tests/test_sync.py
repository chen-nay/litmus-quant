"""同步编排的测试：倒序、整月成败、断点续传、并发不超限。用假 client，不联网。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import DAILY_DATASET, REQUIRED_APIS, DataSync, SyncError

CODES = ("600519.SH", "000001.SZ")
MONTHS: dict[str, list[str]] = {
    "2026-07": ["20260701", "20260702"],
    "2026-08": ["20260803", "20260804"],
    "2026-09": ["20260901"],
}


def rows_for(api_name: str, day: str) -> list[dict]:
    if api_name == "daily":
        return [
            {
                "ts_code": code,
                "trade_date": day,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "pct_chg": 1.0,
                "vol": 1000.0,
                "amount": 1050.0,
            }
            for code in CODES
        ]
    if api_name == "adj_factor":
        return [{"ts_code": c, "trade_date": day, "adj_factor": 1.0} for c in CODES]
    if api_name == "daily_basic":
        return [
            {
                "ts_code": code,
                "trade_date": day,
                "turnover_rate": 2.5,
                "pe_ttm": 30.0,
                "pb": 8.0,
                "ps_ttm": 12.0,
                "dv_ttm": 1.5,
                "total_mv": 12345.0,
                "circ_mv": 10000.0,
            }
            for code in CODES
        ]
    if api_name == "stk_limit":
        return [
            {"ts_code": c, "trade_date": day, "up_limit": 11.55, "down_limit": 9.45} for c in CODES
        ]
    raise AssertionError(f"测试没准备 {api_name} 的数据")


class FakeClient:
    """按剧本返回数据，同时记录调用顺序与并发峰值。"""

    def __init__(
        self,
        *,
        empty: set[tuple[str, str]] | None = None,
        fail_on: tuple[str, str] | None = None,
        delay: float = 0.0,
        no_st: bool = False,
    ):
        self.empty = empty or set()  # (api, day)：这一格没数据
        self.fail_on = fail_on
        self.delay = delay
        self.no_st = no_st
        self.calls: list[tuple[str, str]] = []
        self.calendar_params: dict = {}
        self.max_concurrent = 0
        self._active = 0
        self._lock = threading.Lock()

    def call(self, api_name: str, params: dict | None = None, fields: str | None = None):
        if api_name == "trade_cal":
            self.calendar_params = dict(params or {})
            return [{"cal_date": d} for days in MONTHS.values() for d in days]

        if api_name == "stock_st":
            start = (params or {})["start_date"]
            with self._lock:
                self.calls.append((api_name, start))
            return [] if self.no_st else [{"ts_code": CODES[0], "trade_date": start}]

        day = (params or {})["trade_date"]
        with self._lock:
            self.calls.append((api_name, day))
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if (api_name, day) == self.fail_on:
                raise RuntimeError("网络炸了")
            if (api_name, day) in self.empty:
                return []
            return rows_for(api_name, day)
        finally:
            with self._lock:
                self._active -= 1


def all_days_empty(day: str) -> set[tuple[str, str]]:
    return {(api, day) for api in ("daily", "adj_factor", "daily_basic", "stk_limit")}


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path)


def run(store: MarketStore, client: FakeClient, **kwargs):
    sync = DataSync(client, store, workers=kwargs.pop("workers", 4))
    manifest = Manifest.load(store)
    results = sync.sync_daily("20260701", "20260930", manifest, **kwargs)
    return results, manifest


# ── 倒序与落盘 ──────────────────────────────────────────────────


def test_从最近的月份开始往回拉(store):
    results, _ = run(store, FakeClient())
    assert [r.month for r in results] == ["2026-09", "2026-08", "2026-07"]


def test_每个月都落盘并记账(store):
    _, manifest = run(store, FakeClient())
    assert store.months(DAILY_DATASET) == ("2026-07", "2026-08", "2026-09")
    assert manifest.recorded_months(DAILY_DATASET) == ("2026-07", "2026-08", "2026-09")
    assert manifest.month(DAILY_DATASET, "2026-08").rows == 4  # 2 天 × 2 只
    assert manifest.data_through(DAILY_DATASET) == "2026-09-01"


def test_每个交易日四个接口都要调(store):
    client = FakeClient()
    run(store, client)
    per_day = [call for call in client.calls if call[0] in REQUIRED_APIS]
    assert len(per_day) == 5 * 4  # 5 个交易日 × 4 个接口


def test_ST名单按月拉一次而不是按天(store):
    client = FakeClient()
    run(store, client)

    st_calls = [call for call in client.calls if call[0] == "stock_st"]
    assert len(st_calls) == 3  # 三个月，各拉一次


def test_面板里带上ST标记(store):
    run(store, FakeClient())

    panel = store.read_month(DAILY_DATASET, "2026-08")
    assert panel.get_column("is_st").sum() == 1


def test_一个ST都没拉到就停下(store):
    """全市场一只 ST 都没有不可能，多半是接口出问题，不能把全市场标成非 ST。"""
    client = FakeClient(no_st=True)

    with pytest.raises(SyncError, match="ST"):
        DataSync(client, store, workers=2).sync_daily("20260701", "20260930", Manifest.load(store))


def test_进度按月回调(store):
    seen: list[tuple[str, int, int]] = []
    run(store, FakeClient(), on_month=lambda r, i, n: seen.append((r.month, i, n)))
    assert seen == [("2026-09", 1, 3), ("2026-08", 2, 3), ("2026-07", 3, 3)]


# ── 并发 ────────────────────────────────────────────────────────


def test_并发不超过设定路数(store):
    client = FakeClient(delay=0.01)
    run(store, client, workers=3)
    assert client.max_concurrent <= 3


def test_确实是并发而不是串行(store):
    client = FakeClient(delay=0.01)
    run(store, client, workers=4)
    assert client.max_concurrent > 1


# ── 整月成败 ────────────────────────────────────────────────────


def test_中途失败整月不落盘也不记账(store):
    client = FakeClient(fail_on=("daily_basic", "20260804"))
    sync = DataSync(client, store, workers=2)
    manifest = Manifest.load(store)

    with pytest.raises(RuntimeError, match="网络炸了"):
        sync.sync_daily("20260701", "20260930", manifest)

    assert not store.has_month(DAILY_DATASET, "2026-08")
    assert manifest.month(DAILY_DATASET, "2026-08") is None
    assert store.has_month(DAILY_DATASET, "2026-09")  # 失败前完成的月份保住了


def test_更早的日子只拿到部分接口直接报错(store):
    """不是最近那个交易日，说明数据源不一致，宁可停下也不落一个缺列的月份。"""
    client = FakeClient(empty={("stk_limit", "20260804")})
    sync = DataSync(client, store, workers=2)

    with pytest.raises(SyncError, match="只拿到部分接口"):
        sync.sync_daily("20260701", "20260930", Manifest.load(store))

    assert not store.has_month(DAILY_DATASET, "2026-08")


def test_最近那个交易日只发布了一部分_跳过这天_前面的照常落盘(store):
    """白天同步：涨跌停价 8:40 就有，行情、每日指标收盘后才有（2026-09-15 实测）。"""
    client = FakeClient(empty={("daily", "20260804"), ("daily_basic", "20260804")})
    manifest = Manifest.load(store)

    DataSync(client, store, workers=2).sync_daily("20260701", "20260804", manifest)

    record = manifest.month(DAILY_DATASET, "2026-08")
    assert (record.complete, record.days) == (False, 1)  # 没走完，下次同步整月重来
    assert store.has_month(DAILY_DATASET, "2026-08")


# ── 还没发布的日子 ──────────────────────────────────────────────


def test_某天四个接口都没数据视为还没发布(store):
    """收盘前问今天就是这个情形：跳过这天，这个月记为没走完。"""
    client = FakeClient(empty=all_days_empty("20260804"))
    _, manifest = run(store, client)

    record = manifest.month(DAILY_DATASET, "2026-08")
    assert record.complete is False
    assert record.days == 1
    assert store.has_month(DAILY_DATASET, "2026-08")  # 已有的那天照常落盘


def test_整月都没数据时不写空文件(store):
    client = FakeClient(empty=all_days_empty("20260901"))
    results, manifest = run(store, client)

    september = next(r for r in results if r.month == "2026-09")
    assert september.rows == 0
    assert september.complete is False
    assert not store.has_month(DAILY_DATASET, "2026-09")
    assert manifest.month(DAILY_DATASET, "2026-09") is None


# ── 断点续传 ────────────────────────────────────────────────────


def test_交易日历按整月取(store):
    """请求区间截在月中，日历也要问整月——否则没法判断这个月是不是拉全了。"""
    client = FakeClient()
    DataSync(client, store, workers=4).sync_daily("20260715", "20260820", Manifest.load(store))

    assert client.calendar_params["start_date"] == "20260701"
    assert client.calendar_params["end_date"] == "20260831"
    assert ("daily", "20260701") in client.calls  # 在请求区间外，但属于同一个月，照样要拉


def test_区间截在月中时该月不算走完(store):
    client = FakeClient()
    manifest = Manifest.load(store)
    DataSync(client, store, workers=4).sync_daily("20260701", "20260803", manifest)

    august = manifest.month(DAILY_DATASET, "2026-08")
    assert august.days == 1
    assert august.complete is False  # 8 月 4 号还没拉，不能记成整月
    assert manifest.month(DAILY_DATASET, "2026-07").complete is True


def test_end之后的交易日不白调(store):
    """交易日历是提前发布的，未来的日子还没有数据。"""
    client = FakeClient()
    DataSync(client, store, workers=4).sync_daily("20260701", "20260803", Manifest.load(store))

    assert ("daily", "20260804") not in client.calls
    assert ("daily", "20260901") not in client.calls


def test_已走完的月份不再重拉(store):
    run(store, FakeClient())

    again = FakeClient()
    run(store, again)
    assert again.calls == []  # 除了交易日历，一次都没再调


def test_没走完的月份下次继续拉(store):
    run(store, FakeClient(empty=all_days_empty("20260804")))

    again = FakeClient()  # 这次数据齐了
    results, manifest = run(store, again)

    assert [r.month for r in results] == ["2026-08"]
    assert manifest.month(DAILY_DATASET, "2026-08").complete is True
    assert manifest.month(DAILY_DATASET, "2026-08").days == 2


def test_跨月时先补完没走完的旧月份再拉新月份(store):
    """先写 9 月的话，8 月补完之前 8 月月末就是夹在中间的缺口，status() 会退回不能提问。"""
    DataSync(FakeClient(), store, workers=2).sync_daily(
        "20260701", "20260803", Manifest.load(store)
    )

    results, _ = run(store, FakeClient())

    assert [r.month for r in results] == ["2026-08", "2026-09"]


def test_失败后重跑只补没完成的月份(store):
    client = FakeClient(fail_on=("daily", "20260702"))
    with pytest.raises(RuntimeError):
        DataSync(client, store, workers=2).sync_daily("20260701", "20260930", Manifest.load(store))

    resumed = FakeClient()
    results, _ = run(store, resumed)
    assert [r.month for r in results] == ["2026-07"]
    assert store.months(DAILY_DATASET) == ("2026-07", "2026-08", "2026-09")
