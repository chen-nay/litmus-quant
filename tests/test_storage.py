"""本地落盘的测试：原子写入、按月分片、越界与空数据一律报错。全部离线。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from litmus.data.storage import (
    DATA_DIR_ENV,
    MarketStore,
    MissingDataError,
    StorageError,
    default_root,
    read_json,
    write_atomic,
    write_json,
)


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path)


def panel(month: str = "2026-09", days: tuple[int, ...] = (1,), code: str = "600519.SH"):
    year, mon = int(month[:4]), int(month[5:])
    return pl.DataFrame(
        {
            "date": [date(year, mon, d) for d in days],
            "code": [code] * len(days),
            "close": [10.0 + d for d in days],
            "is_limit_up": [False] * len(days),
        }
    )


# ── 读写往返 ────────────────────────────────────────────────────


def test_写完能原样读回(store):
    store.write_month("daily", "2026-09", panel(days=(1, 2, 3)))
    got = store.read_month("daily", "2026-09")
    assert got.height == 3
    assert got["close"].to_list() == [11.0, 12.0, 13.0]


def test_日期和布尔列的类型不丢(store):
    store.write_month("daily", "2026-09", panel())
    got = store.read_month("daily", "2026-09")
    assert got.schema["date"] == pl.Date
    assert got.schema["is_limit_up"] == pl.Boolean


def test_写入自动建目录(store):
    assert not store.market.exists()
    path = store.write_month("board/concept", "2026-09", panel())
    assert path.exists()
    assert path.parent == store.market / "board" / "concept"


def test_月文件按日期和代码排序(store):
    df = pl.DataFrame(
        {
            "date": [date(2026, 9, 2), date(2026, 9, 1), date(2026, 9, 1)],
            "code": ["000001.SZ", "600519.SH", "000001.SZ"],
            "close": [1.0, 2.0, 3.0],
        }
    )
    store.write_month("daily", "2026-09", df)
    got = store.read_month("daily", "2026-09")
    assert got["code"].to_list() == ["000001.SZ", "600519.SH", "000001.SZ"]
    assert got["close"].to_list() == [3.0, 2.0, 1.0]


def test_重写整月直接替换旧内容(store):
    """当月是整月重写，不做追加合并：写第二次就以第二次为准。"""
    store.write_month("daily", "2026-09", panel(days=(1,)))
    store.write_month("daily", "2026-09", panel(days=(1, 2)))
    assert store.read_month("daily", "2026-09").height == 2


def test_整张表的读写(store):
    assert not store.has_table("meta/stock_basic")
    store.write_table("meta/stock_basic", panel())
    assert store.has_table("meta/stock_basic")
    assert store.read_table("meta/stock_basic").height == 1


# ── 原子性 ──────────────────────────────────────────────────────


def test_写到一半出错_旧文件不动也不留临时文件(store):
    store.write_month("daily", "2026-09", panel(days=(1,)))
    path = store.month_path("daily", "2026-09")

    def boom(tmp: Path) -> None:
        tmp.write_bytes(b"only half of a parquet")
        raise RuntimeError("断网了")

    with pytest.raises(RuntimeError, match="断网了"):
        write_atomic(path, boom)

    assert store.read_month("daily", "2026-09").height == 1  # 旧版本还能读
    assert list(path.parent.iterdir()) == [path]  # 没留下临时文件


def test_写json中文不转义能读回(tmp_path):
    payload = {"来源": "tushare", "已同步月份": ["2026-08", "2026-09"]}
    path = write_json(payload, tmp_path / "sub" / "manifest.json")
    assert "来源" in path.read_text(encoding="utf-8")
    assert read_json(path) == payload


# ── 坏数据一律报错 ──────────────────────────────────────────────


def test_拒绝写空表(store):
    empty = pl.DataFrame(schema={"date": pl.Date, "code": pl.String})
    with pytest.raises(StorageError, match="不写空文件"):
        store.write_month("daily", "2026-09", empty)


def test_行不属于该月直接报错(store):
    mixed = pl.concat([panel("2026-09"), panel("2026-10")])
    with pytest.raises(StorageError, match="不属于 2026-09"):
        store.write_month("daily", "2026-09", mixed)


def test_缺date列直接报错(store):
    with pytest.raises(StorageError, match="date"):
        store.write_month("daily", "2026-09", panel().drop("date"))


def test_date列不是日期类型直接报错(store):
    as_text = panel().with_columns(pl.col("date").cast(pl.String))
    with pytest.raises(StorageError, match="日期类型"):
        store.write_month("daily", "2026-09", as_text)


def test_月份格式非法报错(store):
    for bad in ("2026-9", "202609", "2026-13", "2026-09-01"):
        with pytest.raises(StorageError, match="YYYY-MM"):
            store.month_path("daily", bad)


def test_数据集名不能跑出数据目录(store):
    for bad in ("../../etc/passwd", "/etc/passwd", ""):
        with pytest.raises(StorageError, match="非法"):
            store.dataset_dir(bad)


def test_读不存在的月份报错而不是返回空表(store):
    with pytest.raises(MissingDataError, match="2026-09"):
        store.read_month("daily", "2026-09")


def test_读不存在的表报错(store):
    with pytest.raises(MissingDataError, match="meta/stock_basic"):
        store.read_table("meta/stock_basic")


def test_读不存在的json报错(tmp_path):
    with pytest.raises(MissingDataError):
        read_json(tmp_path / "manifest.json")


# ── 已有月份的清点（断点续传要用）──────────────────────────────


def test_列出已落盘的月份并按时间排序(store):
    for month in ("2026-09", "2025-12", "2026-01"):
        store.write_month("daily", month, panel(month))
    assert store.months("daily") == ("2025-12", "2026-01", "2026-09")


def test_没同步过的数据集返回空(store):
    assert store.months("daily") == ()
    assert not store.has_month("daily", "2026-09")


def test_目录里的杂物不算已同步的月份(store):
    store.write_month("daily", "2026-09", panel())
    (store.dataset_dir("daily") / "notes.parquet").write_bytes(b"")
    assert store.months("daily") == ("2026-09",)


# ── 数据目录的位置 ──────────────────────────────────────────────


def test_环境变量指定数据目录(monkeypatch, tmp_path):
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "elsewhere"))
    assert MarketStore.from_env().root == (tmp_path / "elsewhere").resolve()


def test_没设环境变量就用仓库里的data目录(monkeypatch):
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    root = default_root()
    assert root.name == "data"
    assert (root.parent / "pyproject.toml").exists(), "应该落在仓库根目录下"
