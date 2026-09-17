"""DataSync.status() 的测试：能不能提问、数据截至、历史补到哪、缺什么。全部离线。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from litmus.data.fields import CONCEPT_CAPABILITY, SW_INDUSTRY_L2_CAPABILITY
from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import (
    CONCEPT_TABLES,
    DAILY_DATASET,
    FINA_INDICATOR_TABLE,
    REQUIRED_TABLES,
    DataSync,
    months_between,
)


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path)


def put_months(store, manifest, first, last, *, last_day=28, skip=(), incomplete=()):
    """把 first ~ last 的日频月份写盘并记账。最新那个月只到 last_day 号，记为没走完。"""
    months = months_between(f"{first.replace('-', '')}01", f"{last.replace('-', '')}01")
    for month in months:
        if month in skip:
            continue
        newest = month == months[-1]
        year, mon = int(month[:4]), int(month[5:])
        days = (1, last_day if newest else 28)
        df = pl.DataFrame(
            {"date": [date(year, mon, d) for d in days], "code": ["000001.SZ", "000001.SZ"]}
        )
        store.write_month(DAILY_DATASET, month, df)
        manifest.record_month(
            DAILY_DATASET, month, df, complete=not newest and month not in incomplete
        )


def put_tables(store, manifest, names=REQUIRED_TABLES):
    for name in names:
        df = pl.DataFrame({"x": [1]})
        store.write_table(name, df)
        manifest.record_table(name, df)


def ready_manifest(store, first="2024-09", last="2026-09"):
    """最近两年齐全、必需的表都在；最新一天是 last 那个月的 10 号，概念板块没权限。"""
    manifest = Manifest()
    put_months(store, manifest, first, last, last_day=10)
    put_tables(store, manifest)
    manifest.record_capability(CONCEPT_CAPABILITY, False, "需要 6000 积分")
    return manifest


def status(store, manifest):
    manifest.save(store)
    return DataSync(None, store).status()


# ── 能不能提问 ──────────────────────────────────────────────────


def test_什么都没同步时不能提问(store):
    result = DataSync(None, store).status()

    assert not result.ready
    assert "还没有股票日频数据" in result.reason
    assert result.data_through is None
    assert result.history_from is None
    assert result.unlock_months == ()


def test_最近两年齐了就能提问(store):
    result = status(store, ready_manifest(store))

    assert result.ready
    assert result.reason == ""
    assert result.data_through == "2026-09-10"
    assert result.unlock_months == months_between("20240901", "20260901")
    assert len(result.unlock_months) == 25
    assert result.unlock_missing == ()


def test_两年从数据最新一天往回算而不是今天(store):
    """数据旧了照样能问，由页面提示去同步；锚在今天的话，月初和长假里当月没数据会被误判。"""
    result = status(store, ready_manifest(store, first="2019-03", last="2021-03"))

    assert result.ready
    assert result.data_through == "2021-03-10"


def test_倒序同步到一半时不能提问并给出进度(store):
    manifest = Manifest()
    put_months(store, manifest, "2025-10", "2026-09", last_day=10)
    put_tables(store, manifest)

    result = status(store, manifest)

    assert not result.ready
    assert result.unlock_missing == months_between("20240901", "20250901")
    assert "还缺 13 个月" in result.reason
    assert result.history_from == "2025-10-01"


def test_中间缺一个月不能提问(store):
    manifest = Manifest()
    put_months(store, manifest, "2024-09", "2026-09", skip=("2025-05",))
    put_tables(store, manifest)

    result = status(store, manifest)

    assert not result.ready
    assert result.unlock_missing == ("2025-05",)
    assert "2025-05" in result.reason


def test_最新那个月没走完不影响_更早的没走完算缺(store):
    """更早的月份没走完，说明它月末那几天夹在中间缺着。"""
    manifest = Manifest()
    put_months(store, manifest, "2024-09", "2026-09", incomplete=("2025-06",))
    put_tables(store, manifest)

    assert status(store, manifest).unlock_missing == ("2025-06",)


def test_记了账但文件不在算缺(store):
    manifest = ready_manifest(store)
    store.month_path(DAILY_DATASET, "2025-03").unlink()

    result = status(store, manifest)

    assert not result.ready
    assert result.unlock_missing == ("2025-03",)


def test_缺必需的表不能提问(store):
    manifest = ready_manifest(store)
    store.table_path(FINA_INDICATOR_TABLE).unlink()

    result = status(store, manifest)

    assert not result.ready
    assert FINA_INDICATOR_TABLE in result.reason


def test_只跑过日频的目录不能提问(store):
    manifest = Manifest()
    put_months(store, manifest, "2024-09", "2026-09")

    result = status(store, manifest)

    assert not result.ready
    assert result.unlock_missing == ()
    assert "缺少" in result.reason


def test_概念板块可用时它的表也要在(store):
    manifest = ready_manifest(store)
    manifest.record_capability(CONCEPT_CAPABILITY, True)

    assert not status(store, manifest).ready

    put_tables(store, manifest, CONCEPT_TABLES)
    assert status(store, manifest).ready


def test_概念板块不可用时不拦提问并给出原因(store):
    result = status(store, ready_manifest(store))

    assert result.ready
    assert result.unavailable == {
        SW_INDUSTRY_L2_CAPABILITY: "还没同步过，同步一次后可用",
        CONCEPT_CAPABILITY: "需要 6000 积分",
    }


def test_没探测过的能力按不可用算(store):
    manifest = ready_manifest(store)
    del manifest.capabilities[CONCEPT_CAPABILITY]
    manifest.record_capability(SW_INDUSTRY_L2_CAPABILITY, True)

    assert status(store, manifest).unavailable == {CONCEPT_CAPABILITY: "还没探测过这项数据"}


# ── 历史补到哪 ──────────────────────────────────────────────────


def test_连续补到2016年1月才算历史补完(store):
    result = status(store, ready_manifest(store, first="2016-01"))

    assert result.history_done
    assert result.history_from == "2016-01-01"


def test_历史从最新往回连续数_遇到缺口就停(store):
    """缺口在两年之外：能提问，但历史没补完，页面显示补到缺口之后。"""
    manifest = Manifest()
    put_months(store, manifest, "2016-01", "2026-09", skip=("2020-05",))
    put_tables(store, manifest)

    result = status(store, manifest)

    assert result.ready
    assert not result.history_done
    assert result.history_from == "2020-06-01"


# ── 同步时间 ────────────────────────────────────────────────────


def test_各表与按月数据集都有最近同步时间(store):
    result = status(store, ready_manifest(store))

    assert set(result.synced_at) == {*REQUIRED_TABLES, DAILY_DATASET}
