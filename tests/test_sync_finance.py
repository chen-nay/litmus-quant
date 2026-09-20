"""财务与事件同步的测试：按报告期拉、三张表各自落盘、字段显式请求。不联网。

限售解禁（share_float）P0 不同步，原因见 finance.py 的 normalize_share_float。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import (
    DISCLOSURE_TABLE,
    FINA_INDICATOR_TABLE,
    FORECAST_TABLE,
    DataSync,
    SyncError,
    next_report_period,
    report_periods,
)

START, END = "20240101", "20250630"


def fina_rows(period: str) -> list[dict]:
    return [
        {
            "ts_code": "600519.SH",
            "ann_date": f"{period[:4]}0428",
            "end_date": period,
            "roe": 12.5,
            "roe_yearly": 50.0,
            "or_yoy": 18.3,
            "netprofit_yoy": 22.1,
            "update_flag": "1",
        }
    ]


def forecast_rows(period: str) -> list[dict]:
    return [
        {
            "ts_code": "600519.SH",
            "ann_date": f"{period[:4]}0115",
            "end_date": period,
            "type": "预增",
            "p_change_min": 30.0,
            "p_change_max": 50.0,
        }
    ]


def disclosure_rows(period: str) -> list[dict]:
    return [
        {
            "ts_code": "600519.SH",
            "ann_date": f"{period[:4]}0401",
            "end_date": period,
            "pre_date": f"{period[:4]}0425",
            "actual_date": f"{period[:4]}0428",
        }
    ]


class FakeClient:
    def __init__(self, *, empty: set[str] | None = None):
        self.empty = empty or set()
        self.calls: list[tuple[str, dict, str | None]] = []
        self.peak: dict[str, int] = {}  # 每个接口同时在飞的最大请求数
        self._active: dict[str, int] = {}
        self._lock = threading.Lock()

    def call(self, api_name: str, params: dict | None = None, fields: str | None = None):
        params = dict(params or {})
        with self._lock:
            self.calls.append((api_name, params, fields))
            self._active[api_name] = self._active.get(api_name, 0) + 1
            self.peak[api_name] = max(self.peak.get(api_name, 0), self._active[api_name])
        try:
            time.sleep(0.005)  # 给并发留出重叠的机会，否则峰值永远是 1
            return self._rows(api_name, params)
        finally:
            with self._lock:
                self._active[api_name] -= 1

    def _rows(self, api_name: str, params: dict):
        if api_name in self.empty:
            return []
        if api_name == "fina_indicator_vip":
            return fina_rows(params["period"])
        if api_name == "forecast_vip":
            return forecast_rows(params["period"])
        if api_name == "disclosure_date":
            return disclosure_rows(params["end_date"])
        raise AssertionError(f"测试没准备 {api_name}")

    def params_for(self, api_name: str) -> list[dict]:
        return [params for name, params, _ in self.calls if name == api_name]

    def fields_for(self, api_name: str) -> str | None:
        return next(fields for name, _, fields in self.calls if name == api_name)


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path)


def run(store: MarketStore, client: FakeClient):
    manifest = Manifest.load(store)
    written = DataSync(client, store, workers=4).sync_finance(START, END, manifest)
    return written, manifest


# ── 报告期 ──────────────────────────────────────────────────────


def test_一年四个报告期():
    assert report_periods("20240101", "20241231") == [
        "20240331",
        "20240630",
        "20240930",
        "20241231",
    ]


def test_还没到的报告期不拉():
    """今天是 6 月，三季报和年报还不存在。"""
    assert report_periods("20240101", "20240630") == ["20240331", "20240630"]


def test_跨年的报告期按时间排():
    assert report_periods(START, END) == [
        "20240331",
        "20240630",
        "20240930",
        "20241231",
        "20250331",
        "20250630",
    ]


def test_区间太窄时没有报告期():
    assert report_periods("20240401", "20240629") == []


def test_下一个报告期():
    assert next_report_period("20250913") == "20250930"
    assert next_report_period("20250930") == "20251231"  # 当天就是报告期末，它已经在区间里了
    assert next_report_period("20251231") == "20260331"


# ── 拉取方式 ────────────────────────────────────────────────────


def test_财务和预告按报告期各拉一次(store):
    client = FakeClient()
    run(store, client)

    assert [p["period"] for p in client.params_for("fina_indicator_vip")] == report_periods(
        START, END
    )
    # 预告多一个 END 之后的报告期，理由见下一条
    assert [p["period"] for p in client.params_for("forecast_vip")] == [
        *report_periods(START, END),
        "20250930",
    ]


def test_业绩预告多拉下一个报告期(store):
    """预告在报告期结束前就发：实测 8/1~9/13 公告的预告里有 30 条属于 9/30 的三季报，
    只拉「报告期 <= 今天」会全部漏掉。财务指标和披露计划没有这个问题，不多拉。"""
    client = FakeClient()
    run(store, client)

    assert "20250930" in [p["period"] for p in client.params_for("forecast_vip")]
    assert "20250930" not in [p["period"] for p in client.params_for("fina_indicator_vip")]
    assert "20250930" not in [p["end_date"] for p in client.params_for("disclosure_date")]


def test_披露计划用报告期作为end_date(store):
    client = FakeClient()
    run(store, client)

    assert [p["end_date"] for p in client.params_for("disclosure_date")] == report_periods(
        START, END
    )


def test_解禁接口P0不拉(store):
    """share_float 数据量过大，推迟到 P1，同步时一次都不该调到它。"""
    client = FakeClient()
    run(store, client)

    assert client.params_for("share_float") == []


def test_财务接口并发拉(store):
    """fina_indicator_vip 单次要十几秒，串行四十多个报告期要八分半，必须并发。
    哪个接口扛不住（实测 disclosure_date）由 client 的自适应并发自己降下来，这里不点名串行。"""
    client = FakeClient()
    run(store, client)

    assert client.peak["fina_indicator_vip"] > 1


def test_披露计划一个报告期都不少(store):
    client = FakeClient()
    run(store, client)

    assert len(client.params_for("disclosure_date")) == len(report_periods(START, END))


def test_财务指标只请求需要的字段(store):
    """输出有上百列，全要会让载荷大一个数量级。"""
    client = FakeClient()
    run(store, client)

    requested = client.fields_for("fina_indicator_vip")
    assert "netprofit_yoy" in requested
    assert "roe_yearly" in requested
    assert "update_flag" in requested  # 不在默认输出里，不点名就拿不到
    assert "eps" not in requested


# ── 落盘与记账 ──────────────────────────────────────────────────


def test_三张表各自落盘(store):
    run(store, FakeClient())

    assert store.has_table(FINA_INDICATOR_TABLE)
    for table in (FORECAST_TABLE, DISCLOSURE_TABLE):
        assert store.has_table(table)
        assert store.table_path(table).is_relative_to(store.market / "events")


def test_事件不进日频面板(store):
    """事件是稀疏标记，塞进面板等于每天 5500 行里 99% 是 False。"""
    run(store, FakeClient())
    assert store.months("daily") == ()


def test_每个报告期的数据都合并进一张表(store):
    written, _ = run(store, FakeClient())
    assert written[FINA_INDICATOR_TABLE] == len(report_periods(START, END))


def test_记账里有覆盖的报告期区间(store):
    _, manifest = run(store, FakeClient())

    assert manifest.tables[FINA_INDICATOR_TABLE].note == "20240331~20250630"
    assert manifest.tables[DISCLOSURE_TABLE].rows == len(report_periods(START, END))


def test_某个接口一行都没拉到就停下(store):
    client = FakeClient(empty={"forecast_vip"})

    with pytest.raises(SyncError, match="不覆盖"):
        run(store, client)
