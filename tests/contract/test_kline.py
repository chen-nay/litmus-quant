"""K 线（前复权）在本地真实数据上的契约测试：读数据那一层和 GET /api/stocks/{code}/kline。

数据量都很小：一只股票最多一年多。没有本地数据就整个跳过。
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from litmus.api import Services, SyncJob, create_app
from litmus.data import DataService, DataSync, MarketStore, MissingDataError
from litmus.signals import load_events
from litmus.store import JsonStore

D = date
_market = MarketStore.from_env()
_ds = DataService(_market)
try:
    _first, _last = _ds.data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的股票日频数据", allow_module_level=True)
if _first > D(2025, 1, 2) or _last < D(2026, 9, 11):
    pytest.skip(
        f"用例要 2025-01-02 ~ 2026-09-11 的数据，本地只有 {_first} ~ {_last}",
        allow_module_level=True,
    )


def ratios(values: list[float]) -> list[float]:
    return [after / before for before, after in zip(values, values[1:], strict=False)]


def test_前复权_基准日就是真实价格_涨跌幅和后复权一样():
    kline = _ds.get_kline("600519.SH", D(2025, 1, 1), _last)
    rows = kline.rows
    assert kline.base_date == rows.get_column("date")[-1]
    last = rows.row(-1, named=True)
    assert last["close"] == pytest.approx(last["close_raw"])

    stored = _ds.get_fields(["600519.SH"], D(2025, 1, 1), _last, ["close", "close_raw"])
    assert rows.get_column("date").to_list() == stored.get_column("date").to_list()
    assert ratios(rows.get_column("close").to_list()) == pytest.approx(
        ratios(stored.get_column("close").to_list())
    )
    assert rows.get_column("close_raw").to_list() == pytest.approx(
        stored.get_column("close_raw").to_list()
    )


def test_送转当天_真实价格断崖_前复权照样连续():
    """000034.SZ 2026-05-19 送转（复权因子 ×1.40）：真实收盘 41.57 → 30.96，当天实际涨了 4.45%。"""
    rows = _ds.get_kline("000034.SZ", D(2026, 5, 18), D(2026, 5, 19)).rows
    before, after = rows.row(0, named=True), rows.row(1, named=True)
    assert (before["close_raw"], after["close_raw"]) == pytest.approx((41.57, 30.96))
    assert after["close"] / before["close"] - 1 == pytest.approx(after["pct_chg"] / 100, abs=1e-3)
    assert after["open_raw"] <= after["high_raw"] and after["low_raw"] <= after["close_raw"]


def test_区间里还没上市_返回空表():
    kline = _ds.get_kline("001232.SZ", D(2025, 1, 1), D(2025, 3, 31))  # 2026-08-04 才上市
    assert kline.rows.is_empty()


@pytest.fixture
def client(tmp_path) -> TestClient:
    def no_sync():
        raise AssertionError("契约测试不同步")

    services = Services(
        ds=_ds,
        store=JsonStore(tmp_path),
        events=load_events(),
        sync_job=SyncJob(no_sync),
        data_status=DataSync(None, _market).status,
    )
    return TestClient(create_app(services))


def test_接口返回前复权K线(client):
    body = client.get("/api/stocks/600519.SH/kline?from=2026-09-01&to=2026-09-11").json()
    assert (body["code"], body["adjust"], body["range"]) == (
        "600519.SH",
        "前复权",
        ["2026-09-01", "2026-09-11"],
    )
    assert body["name"] == "贵州茅台"
    assert body["rows"][0]["date"] == "2026-09-01" and body["rows"][-1]["date"] == "2026-09-11"
    assert set(body["rows"][0]) == {
        "date", "open", "high", "low", "close",
        "open_raw", "high_raw", "low_raw", "close_raw", "amount", "pct_chg",
    }  # fmt: skip


def test_接口参数写错或没有这只股票(client):
    assert client.get("/api/stocks/600519.SH/kline?from=20260901&to=2026-09-11").status_code == 400
    assert (
        client.get("/api/stocks/600519.SH/kline?from=2026-09-11&to=2026-09-01").status_code == 400
    )
    assert client.get("/api/stocks/600519.SH/kline?to=2026-09-11").status_code == 400
    assert (
        client.get("/api/stocks/600519.XX/kline?from=2026-09-01&to=2026-09-11").status_code == 404
    )
