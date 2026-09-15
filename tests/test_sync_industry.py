"""申万行业同步的测试：先清单后按行业、一级二级都拉、历史归属不能漏、三张表落盘。不联网。"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from litmus.data.fields import SW_INDUSTRY_L2_CAPABILITY
from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import (
    SW_DAILY_TABLE,
    SW_INDUSTRY_TABLE,
    SW_MEMBER_TABLE,
    DataSync,
    SyncError,
)

START, END = "20160101", "20260913"
INDUSTRIES = [("801010.SI", "农林牧渔"), ("801050.SI", "有色金属")]
#: 二级行业：(代码, 名称, 上级一级代码)
L2_INDUSTRIES = [("801011.SI", "种植业", "801010.SI"), ("801051.SI", "金属新材料", "801050.SI")]
NAMES = {code: name for code, name, *_ in INDUSTRIES + L2_INDUSTRIES}
#: 接口里的行业代码：parent_code 填的是上级的行业代码，不是指数代码
INDUSTRY_CODES = {
    "801010.SI": "110000",
    "801050.SI": "240000",
    "801011.SI": "110100",
    "801051.SI": "240100",
}


def classify_rows(level: str, *, orphan: bool = False) -> list[dict]:
    """按 level 分次返回，和真接口一致。orphan：二级的上级代码对不上任何一级。"""
    if level == "L1":
        rows = [(code, name, "0") for code, name in INDUSTRIES]
    else:
        rows = [
            (code, name, "999999" if orphan else INDUSTRY_CODES[parent])
            for code, name, parent in L2_INDUSTRIES
        ]
    return [
        {
            "index_code": code,
            "industry_name": name,
            "level": level,
            "industry_code": INDUSTRY_CODES[code],
            "parent_code": parent,
            "src": "SW2021",
        }
        for code, name, parent in rows
    ]


def member_rows(l1_code: str, is_new: str) -> list[dict]:
    """按 is_new 分流，和真接口一致：Y 只给当前成分，N 只给已经调出的。

    假 client 必须照着真接口的语义分流。之前它不管传什么都返回两种行，
    结果「只拉 N」这个 bug 在离线测试里完全看不出来，只有真数据才暴露。
    """
    l2_code, l2_name, _ = next(row for row in L2_INDUSTRIES if row[2] == l1_code)
    industry = {
        "l1_code": l1_code,
        "l1_name": NAMES[l1_code],
        "l2_code": l2_code,
        "l2_name": l2_name,
    }
    if is_new == "Y":
        return [{**industry, "ts_code": "600547.SH", "in_date": "20030826", "out_date": None}]
    return [{**industry, "ts_code": "000001.SZ", "in_date": "19910403", "out_date": "20220728"}]


def sw_daily_rows(ts_code: str) -> list[dict]:
    return [
        {
            "ts_code": ts_code,
            "trade_date": day,
            "name": NAMES[ts_code],
            "open": 2986.75,
            "high": 3000.0,
            "low": 2940.0,
            "close": 2946.60,
            "pct_change": -1.34,
            "amount": 83532.0,
            "pb": 2.66,
            "float_mv": 500000.0,
            "total_mv": 800000.0,
        }
        for day in ("20260910", "20260911")
    ]


class FakeClient:
    def __init__(self, *, empty: set[str] | None = None, orphan: bool = False):
        self.empty = empty or set()
        self.orphan = orphan
        self.calls: list[tuple[str, dict, str | None]] = []

    def call(self, api_name: str, params: dict | None = None, fields: str | None = None):
        params = dict(params or {})
        self.calls.append((api_name, params, fields))
        if api_name in self.empty:
            return []
        if api_name == "index_classify":
            return classify_rows(params["level"], orphan=self.orphan)
        if api_name == "index_member_all":
            return member_rows(params["l1_code"], params["is_new"])
        if api_name == "sw_daily":
            return sw_daily_rows(params["ts_code"])
        raise AssertionError(f"测试没准备 {api_name}")

    def params_for(self, api_name: str) -> list[dict]:
        return [params for name, params, _ in self.calls if name == api_name]


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path)


def run(store: MarketStore, client: FakeClient):
    manifest = Manifest.load(store)
    written = DataSync(client, store, workers=4).sync_industry(START, END, manifest)
    return written, manifest


# ── 拉取方式 ────────────────────────────────────────────────────


def test_先拉清单再按行业拉(store):
    client = FakeClient()
    run(store, client)

    assert len(client.params_for("index_classify")) == 2  # 一级、二级各一次
    assert len(client.params_for("index_member_all")) == len(INDUSTRIES) * 2  # 当前 + 已调出
    assert len(client.params_for("sw_daily")) == len(INDUSTRIES) + len(L2_INDUSTRIES)


def test_清单按一级二级各问一次(store):
    client = FakeClient()
    run(store, client)

    params = client.params_for("index_classify")
    assert [p["level"] for p in params] == ["L1", "L2"]
    assert all(p["src"] == "SW2021" for p in params)


def test_当前成分和已调出的都要拉(store):
    """is_new 是过滤器不是开关：Y 只给当前、N 只给已调出。缺哪边都是错的——
    只拉 Y 丢历史（幸存者偏差），只拉 N 丢现在（行业筛选全空）。"""
    client = FakeClient()
    run(store, client)

    for code, _ in INDUSTRIES:
        flags = sorted(
            p["is_new"] for p in client.params_for("index_member_all") if p["l1_code"] == code
        )
        assert flags == ["N", "Y"]


def test_归属表里当前和已调出的都有(store):
    """真数据上踩过的坑：只拉 is_new='N' 时，2011 行全部带 out_date、
    一条当前成分都没有，而行业筛选恰恰要用当前成分。"""
    run(store, FakeClient())

    members = store.read_table(SW_MEMBER_TABLE)
    assert members.filter(members["out_date"].is_null()).height > 0, "一条当前成分都没有"
    assert members.filter(members["out_date"].is_not_null()).height > 0, "一条历史归属都没有"


def test_行业日线一次覆盖整个区间(store):
    """sw_daily 单次 4000 行，十年才 2600 个交易日，一个行业一次调用就够。"""
    client = FakeClient()
    run(store, client)

    for params in client.params_for("sw_daily"):
        assert params["start_date"] == START
        assert params["end_date"] == END


def test_按清单里的行业代码逐个拉(store):
    client = FakeClient()
    run(store, client)

    # 每个行业会被拉两次（is_new 的 Y 和 N），这里只关心「每个行业都拉到了」；
    # 每个行业拉几次由 test_先拉清单再按行业拉 盯着
    pulled = {p["l1_code"] for p in client.params_for("index_member_all")}
    assert pulled == {code for code, _ in INDUSTRIES}  # 归属按一级拉，每行带着二级
    assert {p["ts_code"] for p in client.params_for("sw_daily")} == set(NAMES)  # 日线两级都拉


# ── 落盘与记账 ──────────────────────────────────────────────────


def test_三张表落在各自的位置(store):
    run(store, FakeClient())

    assert store.table_path(SW_INDUSTRY_TABLE).is_relative_to(store.market / "meta")
    assert store.table_path(SW_MEMBER_TABLE).is_relative_to(store.market / "meta")
    assert store.table_path(SW_DAILY_TABLE).is_relative_to(store.market / "board")


def test_清单里有一级和二级_二级带上级代码(store):
    written, _ = run(store, FakeClient())

    assert written[SW_INDUSTRY_TABLE] == len(INDUSTRIES) + len(L2_INDUSTRIES)
    table = store.read_table(SW_INDUSTRY_TABLE)
    l2 = table.filter(pl.col("level") == "L2")
    assert l2.get_column("parent_code").to_list() == [parent for *_, parent in L2_INDUSTRIES]


def test_归属表一级二级分开存(store):
    run(store, FakeClient())

    members = store.read_table(SW_MEMBER_TABLE)
    assert sorted(members.get_column("level").unique().to_list()) == ["L1", "L2"]


def test_拉到二级清单_记为申万二级行业可用(store):
    """老数据目录没有二级：重新同步过这一步才可用，上层才会去读。"""
    _, manifest = run(store, FakeClient())

    assert manifest.is_available(SW_INDUSTRY_L2_CAPABILITY)


def test_已调出的成分也落盘(store):
    run(store, FakeClient())

    members = store.read_table(SW_MEMBER_TABLE)
    assert members.filter(members["out_date"].is_not_null()).height > 0


def test_行业日线不按月分片(store):
    """31 个行业十年才 8 万行，整张存就够，不值得引入按月分片那套复杂度。"""
    run(store, FakeClient())

    assert store.months("board") == ()
    assert store.has_table(SW_DAILY_TABLE)


def test_记账里有备注(store):
    _, manifest = run(store, FakeClient())

    assert "SW2021" in manifest.tables[SW_INDUSTRY_TABLE].note
    assert "is_new=N" in manifest.tables[SW_MEMBER_TABLE].note
    assert manifest.tables[SW_DAILY_TABLE].note == f"{START}~{END}"


# ── 拉空了就停下 ────────────────────────────────────────────────


def test_二级行业找不到上级就报错_不落盘(store):
    """2026-09-15 实测：接口的 parent_code 是行业代码，没换成指数代码时二级全都找不到上级，页面上分不了组。"""
    with pytest.raises(SyncError, match="找不到上级"):
        run(store, FakeClient(orphan=True))

    assert not store.has_table(SW_INDUSTRY_TABLE)


def test_行业清单为空直接报错(store):
    """清单是后面两步的输入，空了就什么都拉不到，要在这里停住。"""
    client = FakeClient(empty={"index_classify"})

    with pytest.raises(SyncError, match="行业清单"):
        run(store, client)


def test_行业日线为空不覆盖已有的表(store):
    run(store, FakeClient())
    before = store.read_table(SW_DAILY_TABLE).height

    with pytest.raises(SyncError, match="不覆盖"):
        run(store, FakeClient(empty={"sw_daily"}))

    assert store.read_table(SW_DAILY_TABLE).height == before
