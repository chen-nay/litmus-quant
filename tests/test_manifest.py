"""manifest 的测试：记账准确、断点续传算得对、能力开关默认关。全部离线。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from litmus.data.manifest import MANIFEST_VERSION, Manifest
from litmus.data.storage import MarketStore, StorageError, write_json


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path)


def panel(month: str = "2026-09", days: tuple[int, ...] = (1, 2), codes: int = 3):
    year, mon = int(month[:4]), int(month[5:])
    return pl.DataFrame(
        {
            "date": [date(year, mon, d) for d in days for _ in range(codes)],
            "code": [f"00000{i}.SZ" for _ in days for i in range(codes)],
        }
    )


# ── 读写往返 ────────────────────────────────────────────────────


def test_没同步过时得到一个空账本(store):
    manifest = Manifest.load(store)
    assert manifest.first_sync_done is False
    assert manifest.recorded_months("daily") == ()
    assert manifest.data_through("daily") is None


def test_存盘再读回内容不变(store):
    manifest = Manifest.load(store)
    manifest.record_month("daily", "2026-09", panel(), complete=False)
    manifest.record_table("meta/stock_basic", panel(), note="含退市")
    manifest.record_capability("tdx_daily", available=False, reason="需要 6000 积分")
    manifest.first_sync_done = True
    manifest.save(store)

    again = Manifest.load(store)
    assert again.month("daily", "2026-09") == manifest.month("daily", "2026-09")
    assert again.tables["meta/stock_basic"].note == "含退市"
    assert again.unavailable_reason("tdx_daily") == "需要 6000 积分"
    assert again.first_sync_done is True


def test_账本写在数据目录的固定位置(store):
    Manifest.load(store).save(store)
    assert (store.market / "manifest.json").exists()


def test_存盘时间会更新(store):
    manifest = Manifest.load(store)
    assert manifest.updated_at == ""
    manifest.save(store)
    assert manifest.updated_at.startswith("20")


def test_版本对不上要求重新同步(store):
    write_json({"version": MANIFEST_VERSION + 1}, store.manifest_path)
    with pytest.raises(StorageError, match="重新同步"):
        Manifest.load(store)


# ── 按月记账 ────────────────────────────────────────────────────


def test_行数和首末日期从数据本身算(store):
    manifest = Manifest.load(store)
    manifest.record_month("daily", "2026-09", panel(days=(1, 2, 30), codes=5), complete=True)

    record = manifest.month("daily", "2026-09")
    assert record.rows == 15  # 3 天 × 5 只
    assert record.days == 3
    assert record.first_date == "2026-09-01"
    assert record.last_date == "2026-09-30"
    assert record.complete is True


def test_数据截止日取最新一个月的最后一天(store):
    manifest = Manifest.load(store)
    manifest.record_month("daily", "2026-09", panel("2026-09", days=(1, 11)), complete=False)
    manifest.record_month("daily", "2026-08", panel("2026-08", days=(3, 31)), complete=True)
    assert manifest.data_through("daily") == "2026-09-11"


def test_不同数据集各记各的(store):
    manifest = Manifest.load(store)
    manifest.record_month("daily", "2026-09", panel(), complete=True)
    assert manifest.recorded_months("board/concept") == ()
    assert manifest.data_through("board/concept") is None


# ── 断点续传 ────────────────────────────────────────────────────


WANTED = ("2026-07", "2026-08", "2026-09")


def test_没记录过的月份都要拉(store):
    assert Manifest.load(store).missing_months("daily", WANTED) == WANTED


def test_已完成的月份跳过(store):
    manifest = Manifest.load(store)
    manifest.record_month("daily", "2026-07", panel("2026-07"), complete=True)
    assert manifest.missing_months("daily", WANTED) == ("2026-08", "2026-09")


def test_还没走完的当月每次都重拉(store):
    """当月是整月重写，所以只要没走完就得重来一遍。"""
    manifest = Manifest.load(store)
    manifest.record_month("daily", "2026-09", panel("2026-09"), complete=False)
    assert "2026-09" in manifest.missing_months("daily", WANTED)


def test_记录在但文件被删掉了_仍然要重拉(store):
    manifest = Manifest.load(store)
    store.write_month("daily", "2026-07", panel("2026-07"))
    manifest.record_month("daily", "2026-07", panel("2026-07"), complete=True)
    assert manifest.missing_months("daily", WANTED, store) == ("2026-08", "2026-09")

    store.month_path("daily", "2026-07").unlink()
    assert manifest.missing_months("daily", WANTED, store) == WANTED


def test_中断后续传只补没拉完的(store):
    """模拟：拉到一半退出，重启后接着拉。"""
    manifest = Manifest.load(store)
    for month in ("2026-07", "2026-08"):
        store.write_month("daily", month, panel(month))
        manifest.record_month("daily", month, panel(month), complete=True)
    manifest.save(store)

    resumed = Manifest.load(store)
    assert resumed.missing_months("daily", WANTED, store) == ("2026-09",)


# ── 能力探测 ────────────────────────────────────────────────────


def test_没探测过一律按不可用(store):
    manifest = Manifest.load(store)
    assert manifest.is_available("tdx_daily") is False
    assert "还没探测过" in manifest.unavailable_reason("tdx_daily")


def test_探测不通过要记下原因(store):
    manifest = Manifest.load(store)
    manifest.record_capability("tdx_daily", available=False, reason="积分不足，需要 6000")
    assert manifest.is_available("tdx_daily") is False
    assert manifest.unavailable_reason("tdx_daily") == "积分不足，需要 6000"


def test_积分够了之后可以改成可用(store):
    manifest = Manifest.load(store)
    manifest.record_capability("tdx_daily", available=False, reason="积分不足")
    manifest.record_capability("tdx_daily", available=True)
    assert manifest.is_available("tdx_daily") is True
    assert manifest.unavailable_reason("tdx_daily") == ""
