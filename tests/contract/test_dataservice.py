"""DataService 契约测试：在本地真实数据上核对上层依赖的语义（ARCHITECTURE §2.4）。

DataService 不联网，所以不需要 token，只要本地同步过数据；没有就整个跳过。
每条契约用一个真实案例，2026-09-14 从本地数据里找出来，并对着原始表逐条核对过期望值。
数据源以后修正了历史，个别数值可能要跟着改——那正是这些测试该报出来的。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from litmus.data import (
    CONCEPT,
    FIELDS,
    DataService,
    DataSync,
    MissingDataError,
    UnknownFieldError,
    available_targets,
)
from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore

D = date

_store = MarketStore.from_env()
try:
    _first, _last = DataService(_store).data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的股票日频数据", allow_module_level=True)
if _first > D(2016, 1, 4) or _last < D(2026, 9, 11):
    pytest.skip(
        f"契约用例要 2016-01-04 ~ 2026-09-11 的数据，本地只有 {_first} ~ {_last}",
        allow_module_level=True,
    )
needs_concept = pytest.mark.skipif(
    CONCEPT not in available_targets(Manifest.load(_store)), reason="本地概念板块不可用"
)


@pytest.fixture(scope="module")
def ds() -> DataService:
    return DataService(_store)


def by_date(table: pl.DataFrame, column: str) -> dict[date, object]:
    return dict(table.select("date", column).iter_rows())


# ── 返回格式 ────────────────────────────────────────────────────


def test_返回长表_按日期代码排序_类型符合字段目录(ds):
    fields = ["close", "amount", "is_st", "roe", "is_report_date", "is_ex_div", "is_new"]
    start, end = D(2026, 8, 3), D(2026, 9, 11)
    table = ds.get_fields(["600519.SH", "000001.SZ"], start, end, fields)

    assert table.columns == ["date", "code", *fields]
    assert table.equals(table.sort("date", "code"))
    assert table.height == 2 * len(ds.get_trading_calendar(start, end))
    for name in fields:
        assert table.schema[name] == {"float": pl.Float64, "bool": pl.Boolean}[FIELDS[name].dtype]
        if FIELDS[name].dtype == "bool":
            assert table.get_column(name).null_count() == 0


def test_同一查询重复调用结果逐位相同(ds):
    args = (["002594.SZ"], D(2025, 1, 2), D(2025, 12, 31), ["close", "roe", "is_ex_div"])
    assert ds.get_fields(*args).equals(ds.get_fields(*args))


def test_codes传None取全部(ds):
    day = D(2026, 9, 11)
    assert ds.get_fields(None, day, day, ["close"]).height > 5000


def test_停牌日没有行(ds):
    """000004.SZ 2025-04-29 停牌，面板不补齐。"""
    table = ds.get_fields(["000004.SZ"], D(2025, 4, 29), D(2025, 4, 30), ["close"])
    assert table.get_column("date").to_list() == [D(2025, 4, 30)]


def test_各类数据最新到哪天_股票行情就是数据截至(ds):
    latest = {item.key: item.day for item in ds.latest_dates()}
    assert latest["stock"] == ds.data_range()[1]
    assert {"stock", "sw_industry", "index", "index_weight", "finance"} <= set(latest)
    assert latest["index_weight"] <= latest["stock"]  # 指数成分每月发一两次，常常晚几天


def test_最新交易日就是数据截至(ds):
    status = DataSync(None, _store).status()
    assert ds.latest_trading_day().isoformat() == status.data_through


# ── 后复权 ──────────────────────────────────────────────────────


def test_送转前后后复权价格连续(ds):
    """比亚迪 2025-07-29 送转：不复权收盘价 337 → 111.42，后复权价格只按当天涨跌幅变动。"""
    table = ds.get_fields(
        ["002594.SZ"],
        D(2025, 7, 28),
        D(2025, 7, 29),
        ["close", "close_raw", "pct_chg", "is_ex_div"],
    )
    before, after = table.rows(named=True)
    assert after["close_raw"] / before["close_raw"] < 0.4
    assert after["close"] / before["close"] - 1 == pytest.approx(after["pct_chg"] / 100, abs=1e-4)
    assert (before["is_ex_div"], after["is_ex_div"]) == (False, True)


# ── 财务按公告日对齐 ────────────────────────────────────────────


def test_更正只在更正之后生效(ds):
    """中国石化 2026 一季报：04-22 先发旧版本（标 0，8.1924），04-29 出最新版本（8.1788）。"""
    roe = by_date(ds.get_fields(["600028.SH"], D(2026, 4, 21), D(2026, 4, 29), ["roe"]), "roe")
    assert roe[D(2026, 4, 21)] == 3.8551  # 还是 2025 年报
    assert roe[D(2026, 4, 28)] == 8.1924
    assert roe[D(2026, 4, 29)] == 8.1788


def test_旧报告期的更正不覆盖更新的一期(ds):
    """001299.SZ 2026-08-27 同一天更正了一季报（9.1043）、发了半年报（6.8266），取半年报。"""
    roe = by_date(ds.get_fields(["001299.SZ"], D(2026, 8, 26), D(2026, 8, 27), ["roe"]), "roe")
    assert roe == {D(2026, 8, 26): 9.0224, D(2026, 8, 27): 6.8266}


# ── 公告日事件 ──────────────────────────────────────────────────


def test_周末披露的财报标在下一个交易日(ds):
    """301459.SZ 半年报 2026-08-29（周六）披露。"""
    table = ds.get_fields(["301459.SZ"], D(2026, 8, 28), D(2026, 9, 1), ["is_report_date"])
    assert by_date(table, "is_report_date") == {
        D(2026, 8, 28): False,
        D(2026, 8, 31): True,
        D(2026, 9, 1): False,
    }


# ── 交易日历 ────────────────────────────────────────────────────


def test_交易日历只含交易日(ds):
    assert ds.get_trading_calendar(D(2025, 10, 1), D(2025, 10, 8)) == []  # 国庆
    september = ds.get_trading_calendar(D(2026, 9, 1), D(2026, 9, 11))
    assert len(september) == 9
    assert all(day.weekday() < 5 for day in september)


# ── 退市股 ──────────────────────────────────────────────────────


def test_退市股在上市期间查得到_退市后不在池内(ds):
    """*ST华仪（600290.SH）2024-01-16 退市，最后一个交易日 2023-12-25。"""
    table = ds.get_fields(["600290.SH"], D(2023, 12, 1), D(2023, 12, 29), ["close_raw"])
    assert table.get_column("date").max() == D(2023, 12, 25)
    assert "600290.SH" in ds.get_universe(D(2023, 12, 25))
    assert "600290.SH" not in ds.get_universe(D(2023, 12, 25), exclude=["ST"])
    assert "600290.SH" not in ds.get_universe(D(2024, 1, 2))


# ── 按日股票池 ──────────────────────────────────────────────────


def test_变成ST之前在池内_之后被剔除(ds):
    """688189.SH 2026-06-11 起被 ST，前一个交易日还不是。"""
    assert "688189.SH" in ds.get_universe(D(2026, 6, 10), exclude=["ST"])
    assert "688189.SH" not in ds.get_universe(D(2026, 6, 11), exclude=["ST"])
    assert "688189.SH" in ds.get_universe(D(2026, 6, 11))


def test_次新股按上市后的交易日数剔除(ds):
    """陕西旅游（603402.SH）2026-01-06 上市，第 60 个交易日是 04-08。"""
    table = ds.get_fields(["603402.SH"], D(2026, 4, 8), D(2026, 4, 9), ["is_new"])
    assert table.get_column("is_new").to_list() == [True, False]
    assert "603402.SH" not in ds.get_universe(D(2026, 4, 8), exclude=["new_listing_60d"])
    assert "603402.SH" in ds.get_universe(D(2026, 4, 9), exclude=["new_listing_60d"])


def test_行业按当天归属_剔除日当天还算旧行业(ds):
    """000159.SZ：建筑装饰 2024-07-29 剔除，电力设备 07-30 纳入。"""
    assert "000159.SZ" in ds.get_universe(D(2024, 7, 29), industry="建筑装饰")
    assert "000159.SZ" in ds.get_universe(D(2024, 7, 30), industry="电力设备")
    assert "000159.SZ" not in ds.get_universe(D(2024, 7, 30), industry="建筑装饰")


def test_旧归属没关闭时按最新的归属(ds):
    """000595.SZ：机械设备的归属一直没关，2026-07-01 起归公用事业。"""
    assert "000595.SZ" in ds.get_universe(D(2026, 6, 30), industry="机械设备")
    assert "000595.SZ" in ds.get_universe(D(2026, 7, 1), industry="公用事业")
    assert "000595.SZ" not in ds.get_universe(D(2026, 7, 1), industry="机械设备")


def test_指数成分用最近一期快照_早于第一期直接报错(ds):
    with pytest.raises(MissingDataError, match="2016-01-29"):
        ds.get_universe(D(2016, 1, 15), base="hs300")
    assert 250 < len(ds.get_universe(D(2016, 1, 29), base="hs300")) <= 300  # 停牌的不在池内
    assert (
        250 < len(ds.get_universe(D(2026, 9, 1), base="hs300")) <= 300
    )  # 9 月快照还没发，用 8 月底那期


def test_股票池不含北交所(ds):
    assert not any(code.endswith(".BJ") for code in ds.get_universe(D(2026, 9, 11)))


def test_按日股票池与逐日取的一致(ds):
    days = (D(2026, 9, 10), D(2026, 9, 11))
    mask = ds.get_universe_mask(*days, exclude=["ST", "new_listing_60d"])
    for day in days:
        expected = ds.get_universe(day, exclude=["ST", "new_listing_60d"])
        assert mask.filter(pl.col("date") == day).get_column("code").to_list() == expected


def test_非交易日取股票池直接报错(ds):
    with pytest.raises(ValueError, match="不是交易日"):
        ds.get_universe(D(2026, 9, 6))


# ── 板块 ────────────────────────────────────────────────────────


def test_申万行业字段路由到申万日线(ds):
    table = ds.get_fields(
        ["801010.SI"], D(2026, 9, 10), D(2026, 9, 11), ["close", "market_cap"], target="sw_industry"
    )
    assert table.height == 2
    assert table.get_column("market_cap").min() > 0


def test_申万行业清单与成分(ds):
    assert len(ds.list_boards("sw_industry")) == 31
    assert "000001.SZ" in ds.board_members("801780.SI")  # 银行


@needs_concept
def test_概念板块字段路由到通达信日线(ds):
    table = ds.get_fields(
        ["880501.TDX"], D(2026, 9, 10), D(2026, 9, 11), ["limit_up_num"], target="concept"
    )
    assert table.height == 2


@needs_concept
def test_概念板块只有快照日的当前成分(ds):
    assert ds.board_members("880501.TDX")
    with pytest.raises(MissingDataError, match="当前成分"):
        ds.board_members("880501.TDX", as_of=D(2025, 6, 3))


# ── 报错，而不是返回空列 ────────────────────────────────────────


def test_区间超出本地数据直接报错(ds):
    with pytest.raises(MissingDataError, match="只覆盖"):
        ds.get_fields(["600519.SH"], D(2015, 12, 1), D(2016, 1, 8), ["close"])


@needs_concept
def test_概念板块区间早于数据起点直接报错(ds):
    with pytest.raises(MissingDataError, match="只覆盖"):
        ds.get_fields(["880501.TDX"], D(2025, 1, 2), D(2025, 4, 1), ["close"], target="concept")


def test_未知字段与标的不支持的字段直接报错(ds):
    day = D(2026, 9, 11)
    with pytest.raises(UnknownFieldError):
        ds.get_fields(["600519.SH"], day, day, ["close_adj"])
    with pytest.raises(UnknownFieldError):
        ds.get_fields(["801010.SI"], day, day, ["turnover"], target="sw_industry")


def test_可用的标的类型(ds):
    targets = ds.available_targets()
    assert "stock" in targets and "sw_industry" in targets


# ── 涨跌停标记 ──────────────────────────────────────────────────


def test_不设涨跌幅限制的日子涨跌停标记都是False(ds):
    """001232.SZ 2026-08-04 上市首日不设涨跌幅限制：数据源把涨停价填成 100000、跌停价为空。"""
    flags = ["is_limit_up", "is_limit_down", "open_limit_up"]
    row = ds.get_fields(["001232.SZ"], D(2026, 8, 4), D(2026, 8, 4), flags).row(0, named=True)
    assert [row[name] for name in flags] == [False, False, False]


def test_数据源缺了涨跌停价的日子标记为空值(ds):
    """000022.SZ 2018-12-19 没有涨跌停价，判断不了，不猜。"""
    flags = ["is_limit_up", "is_limit_down"]
    row = ds.get_fields(["000022.SZ"], D(2018, 12, 19), D(2018, 12, 19), flags).row(0, named=True)
    assert [row[name] for name in flags] == [None, None]


# ── 往前带行情（表达式引擎预热用）──────────────────────────────


def test_往前带的是每只股票自己的行情_停过牌的也凑够(ds):
    """神州高铁（000008.SZ）过去一年停过牌：按交易日历往前推 250 天只有 247 条，要往更早翻才凑够。"""
    as_of = D(2026, 9, 11)
    table = ds.get_fields(["000008.SZ", "600519.SH"], as_of, as_of, ["close"], lookback=250)
    earlier = table.filter(pl.col("date") < as_of)
    assert dict(earlier.group_by("code").len().iter_rows()) == {"000008.SZ": 250, "600519.SH": 250}

    calendar_start = ds.get_trading_calendar(D(2025, 1, 1), as_of)[-251]
    stock = table.filter(pl.col("code") == "000008.SZ")
    assert stock.get_column("date").min() < calendar_start
    history = ds.get_fields(["000008.SZ"], D(2016, 1, 4), as_of, ["close"])
    assert stock.get_column("close").to_list() == history.get_column("close").tail(251).to_list()


def test_往前带不够就有多少带多少(ds):
    """陕西旅游（603402.SH）2026-01-06 上市，01-09 之前只有 3 条行情。"""
    table = ds.get_fields(["603402.SH"], D(2026, 1, 9), D(2026, 1, 9), ["close"], lookback=10)
    assert table.get_column("date").to_list() == [
        D(2026, 1, 6),
        D(2026, 1, 7),
        D(2026, 1, 8),
        D(2026, 1, 9),
    ]


def test_往前带出来的行现算字段也算对(ds):
    """比亚迪 2025-07-29 送转：从 07-30 往前带 1 条，带出来的 07-29 也要标成除权日。"""
    table = ds.get_fields(["002594.SZ"], D(2025, 7, 30), D(2025, 7, 30), ["is_ex_div"], lookback=1)
    assert by_date(table, "is_ex_div") == {D(2025, 7, 29): True, D(2025, 7, 30): False}


def test_板块也能往前带(ds):
    day = D(2026, 9, 11)
    table = ds.get_fields(["801010.SI"], day, day, ["close"], target="sw_industry", lookback=5)
    assert table.height == 6
    assert table.get_column("date").max() == day


# ── 股票信息与指数 ──────────────────────────────────────────────


def test_股票信息_名称和行业都按当天(ds):
    """000595.SZ：2026-06-30 叫 *ST宝实、属于机械设备；2026-09-11 叫新能股份、属于公用事业。"""
    before = ds.stock_info(["000595.SZ"], D(2026, 6, 30)).row(0, named=True)
    after = ds.stock_info(["000595.SZ"], D(2026, 9, 11)).row(0, named=True)
    assert (before["name"], before["industry"]) == ("*ST宝实", "机械设备")
    assert (after["name"], after["industry"]) == ("新能股份", "公用事业")


def test_股票信息_退市日_查不到的老代码照样返回(ds):
    info = ds.stock_info(["600290.SH", "000022.SZ", "000001.SZ"], D(2018, 12, 19))
    rows = {row["code"]: row for row in info.iter_rows(named=True)}
    assert list(rows) == ["000001.SZ", "000022.SZ", "600290.SH"]
    assert rows["600290.SH"]["delist_date"] == D(2024, 1, 16)
    assert rows["000001.SZ"]["industry"] == "银行"
    assert rows["000022.SZ"]["list_date"] is None  # 股票列表里没有这个老代码


def test_指数日线(ds):
    table = ds.get_index_daily("000300.SH", D(2026, 9, 10), D(2026, 9, 11))
    assert table.columns == ["date", "code", "open", "close"]
    assert table.height == 2
    assert table.get_column("open").min() > 0
    with pytest.raises(ValueError, match="000300.SH"):
        ds.get_index_daily("000016.SH", D(2026, 9, 10), D(2026, 9, 11))
