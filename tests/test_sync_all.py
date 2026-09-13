"""sync_all 编排与命令行参数的测试。不联网。"""

from __future__ import annotations

from pathlib import Path

import pytest

from litmus.data.__main__ import build_parser, load_env
from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import SYNC_STEPS, DataSync, SyncError


class RecordingSync(DataSync):
    """记录哪几步被调用、按什么顺序，不真的拉数据。"""

    def __init__(self):
        self.called: list[str] = []

    def sync_meta(self, start, end, manifest=None):
        self.called.append("meta")
        return {"meta/stock_basic": 1}

    def sync_industry(self, start, end, manifest=None):
        self.called.append("industry")
        return {"meta/sw_industry": 1}

    def sync_concept(self, start, end, manifest=None):
        self.called.append("concept")
        return {"meta/tdx_concept": 1}

    def sync_index(self, start, end, manifest=None):
        self.called.append("index")
        return {"index/daily": 1}

    def sync_finance(self, start, end, manifest=None):
        self.called.append("finance")
        return {"fina_indicator": 1}

    def sync_daily(self, start, end, manifest=None, on_month=None):
        self.called.append("daily")
        return []


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path)


# ── 编排 ────────────────────────────────────────────────────────


def test_默认跑全部步骤(store):
    sync = RecordingSync()
    sync.sync_all("20240101", "20240331", Manifest.load(store))

    assert sync.called == list(SYNC_STEPS)


def test_日频排在最后(store):
    """前四步几分钟就完，daily 要一个多小时，先让便宜的就位。"""
    sync = RecordingSync()
    sync.sync_all("20240101", "20240331", Manifest.load(store))

    assert sync.called[-1] == "daily"


def test_only只跑选中的步骤(store):
    sync = RecordingSync()
    sync.sync_all("20240101", "20240331", Manifest.load(store), steps=["daily"])

    assert sync.called == ["daily"]


def test_顺序由代码决定而不是传参顺序(store):
    """传参顺序是命令行打字的顺序，不该决定执行顺序——步骤之间的先后是有含义的。"""
    sync = RecordingSync()
    sync.sync_all("20240101", "20240331", Manifest.load(store), steps=["daily", "meta"])

    assert sync.called == ["meta", "daily"]


def test_不认识的步骤直接报错(store):
    sync = RecordingSync()

    with pytest.raises(SyncError, match="不认识的同步步骤"):
        sync.sync_all("20240101", "20240331", Manifest.load(store), steps=["daliy"])


def test_摘要里有每一步的结果(store):
    sync = RecordingSync()
    summary = sync.sync_all("20240101", "20240331", Manifest.load(store), steps=["meta", "daily"])

    assert set(summary) == {"meta", "daily"}
    assert summary["daily"] == {"months": 0, "rows": 0}


# ── 命令行参数 ──────────────────────────────────────────────────


def test_默认从2016年开始():
    args = build_parser().parse_args([])
    assert args.start == "20160101"
    assert args.only is None


def test_only可以传多个步骤():
    args = build_parser().parse_args(["--only", "meta", "daily"])
    assert args.only == ["meta", "daily"]


def test_only拼错了会被拦下():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--only", "daliy"])


def test_可以指定区间与并发():
    args = build_parser().parse_args(["--start", "20240101", "--end", "20240331", "--workers", "4"])
    assert (args.start, args.end, args.workers) == ("20240101", "20240331", 4)


# ── .env 加载 ───────────────────────────────────────────────────


def test_读env文件(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# 注释\nTUSHARE_TOKEN=abc\n\nLLM_MODEL = m1\n", encoding="utf-8")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    load_env(env)

    assert os_environ("TUSHARE_TOKEN") == "abc"
    assert os_environ("LLM_MODEL") == "m1"


def test_已有的环境变量不被覆盖(tmp_path, monkeypatch):
    """命令行里显式设过的，优先于 .env。"""
    env = tmp_path / ".env"
    env.write_text("TUSHARE_TOKEN=from_file\n", encoding="utf-8")
    monkeypatch.setenv("TUSHARE_TOKEN", "from_shell")

    load_env(env)

    assert os_environ("TUSHARE_TOKEN") == "from_shell"


def test_没有env文件也不报错(tmp_path):
    load_env(tmp_path / "nonexistent.env")


def os_environ(key: str) -> str | None:
    import os

    return os.environ.get(key)
