"""财务与事件同步的测试：按报告期拉、四张表各自落盘、字段显式请求。不联网。"""

from __future__ import annotations

from pathlib import Path

import pytest

from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import (
    DISCLOSURE_TABLE,
    FINA_INDICATOR_TABLE,
    FORECAST_TABLE,
    SHARE_FLOAT_TABLE,
    DataSync,
    SyncError,
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


def float_rows(start_date: str) -> list[dict]:
    return [
        {
            "ts_code": "600519.SH",
            "ann_date": start_date,
            "float_date": f"{start_date[:4]}0315",
            "float_share": 25076106.0,
            "float_ratio": 1.9041,
            "share_type": "定增股份",
        }
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
        if api_name == "fina_indicator_vip":
            return fina_rows(params["period"])
        if api_name == "forecast_vip":
            return forecast_rows(params["period"])
        if api_name == "disclosure_date":
            return disclosure_rows(params["end_date"])
        if api_name == "share_float":
            return float_rows(params["start_date"])
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


# ── 拉取方式 ────────────────────────────────────────────────────


def test_财务和预告按报告期各拉一次(store):
    client = FakeClient()
    run(store, client)

    assert [p["period"] for p in client.params_for("fina_indicator_vip")] == report_periods(
        START, END
    )
    assert [p["period"] for p in client.params_for("forecast_vip")] == report_periods(START, END)


def test_披露计划用报告期作为end_date(store):
    client = FakeClient()
    run(store, client)

    assert [p["end_date"] for p in client.params_for("disclosure_date")] == report_periods(
        START, END
    )


def test_解禁按年份区间拉而不是报告期(store):
    """解禁日和报告期没关系，按报告期拉会漏掉季度之间解禁的。"""
    client = FakeClient()
    run(store, client)

    windows = client.params_for("share_float")
    assert [w["start_date"] for w in windows] == ["20240101", "20250101"]
    assert [w["end_date"] for w in windows] == ["20241231", "20251231"]


def test_财务指标只请求需要的字段(store):
    """输出有上百列，全要会让载荷大一个数量级。"""
    client = FakeClient()
    run(store, client)

    requested = client.fields_for("fina_indicator_vip")
    assert "netprofit_yoy" in requested
    assert "roe_yearly" in requested
    assert "eps" not in requested


# ── 落盘与记账 ──────────────────────────────────────────────────


def test_四张表各自落盘(store):
    run(store, FakeClient())

    assert store.has_table(FINA_INDICATOR_TABLE)
    for table in (FORECAST_TABLE, DISCLOSURE_TABLE, SHARE_FLOAT_TABLE):
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
    assert manifest.tables[SHARE_FLOAT_TABLE].rows == 2  # 两年各一行


def test_某个接口一行都没拉到就停下(store):
    client = FakeClient(empty={"forecast_vip"})

    with pytest.raises(SyncError, match="不覆盖"):
        run(store, client)
