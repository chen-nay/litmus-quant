"""表达式引擎在本地真实数据上的契约测试：预热、股票池、实际统计起点。

期望值都用 Polars 直接在长历史上另算一遍来对照，不经过表达式引擎。
DataService 不联网，没有本地数据就整个跳过。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from litmus.data import CONCEPT, DataService, MissingDataError
from litmus.expr import ExprDataError, evaluate

D = date
AS_OF = D(2026, 9, 11)

_ds = DataService.from_env()
try:
    _first, _last = _ds.data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的股票日频数据", allow_module_level=True)
if _first > D(2016, 1, 4) or _last < AS_OF:
    pytest.skip(
        f"用例要 2016-01-04 ~ {AS_OF} 的数据，本地只有 {_first} ~ {_last}", allow_module_level=True
    )

YEAR_LINE = "Cross($close, Mean($close, 250))"
VOLUME_UP = "$amount > Mean(Ref($amount, 1), 20) * 2"
MACD = "Cross(EMA($close,12) - EMA($close,26), EMA(EMA($close,12) - EMA($close,26), 9))"
DEFAULT_EXCLUDE = ["ST", "suspended", "new_listing_60d"]


@pytest.fixture(scope="module")
def ds() -> DataService:
    return _ds


def rows_of(ds: DataService, code: str, start: date, end: date) -> pl.DataFrame:
    return ds.get_fields([code], start, end, ["close"]).select("date", "code")


def chosen(result) -> set[str]:
    return set(result.values.filter(pl.col("value")).get_column("code").to_list())


def test_停过牌的股票预热按自己的行情补够():
    """神州高铁（000008.SZ）过去一年停过牌，旧写法按交易日历往前推会凑不够 250 条、年线算不出来。"""
    universe = rows_of(_ds, "000008.SZ", AS_OF, AS_OF)
    result = evaluate("Mean($close, 250)", "sort", universe, AS_OF, AS_OF, _ds)
    history = _ds.get_fields(["000008.SZ"], D(2016, 1, 4), AS_OF, ["close"])
    expected = history.get_column("close").tail(250).mean()
    assert result.values.get_column("value").to_list() == [pytest.approx(expected)]


def test_股票表_放量突破年线和直接在长历史上算的一致(ds):
    pool = ds.get_universe_mask(AS_OF, AS_OF, exclude=DEFAULT_EXCLUDE)
    result = evaluate(f"({YEAR_LINE}) & ({VOLUME_UP})", "filter", pool, AS_OF, AS_OF, ds)

    codes = pool.get_column("code").to_list()
    history = ds.get_fields(codes, D(2020, 1, 2), AS_OF, ["close", "amount"]).sort("code", "date")
    close, amount = pl.col("close"), pl.col("amount")
    ma = close.rolling_mean(250).over("code")
    hit = (
        (close.shift(1).over("code") <= ma.shift(1).over("code"))
        & (close > ma)
        & (amount > amount.shift(1).rolling_mean(20).over("code") * 2)
    )
    expected = history.with_columns(hit.alias("hit")).filter(
        (pl.col("date") == AS_OF) & pl.col("hit")
    )

    assert chosen(result) == set(expected.get_column("code").to_list())
    assert result.values.height == pool.height  # 池内每只都有结果，不在池内的一只都没有
    assert result.values.get_column("value").null_count() == 0  # 筛选结果的空值已判为不满足
    assert result.first_date == AS_OF


def test_MACD预热8n和从2016年完整历史算的一致(ds):
    pool = ds.get_universe_mask(AS_OF, AS_OF)
    result = evaluate(MACD, "filter", pool, AS_OF, AS_OF, ds)

    history = ds.get_fields(pool.get_column("code").to_list(), D(2016, 1, 4), AS_OF, ["close"])
    close = pl.col("close")
    dif = (close.ewm_mean(span=12, adjust=False) - close.ewm_mean(span=26, adjust=False)).over(
        "code"
    )
    frame = history.sort("code", "date").with_columns(dif.alias("dif"))
    frame = frame.with_columns(
        pl.col("dif").ewm_mean(span=9, adjust=False).over("code").alias("dea")
    )
    prev = lambda name: pl.col(name).shift(1).over("code")  # noqa: E731
    golden = (prev("dif") <= prev("dea")) & (pl.col("dif") > pl.col("dea"))
    expected = frame.with_columns(golden.alias("hit")).filter(
        (pl.col("date") == AS_OF) & pl.col("hit")
    )

    assert chosen(result) == set(expected.get_column("code").to_list())


def test_个股回看的实际统计起点是自己的第252条行情(ds):
    """事件包一层「由不满足变为满足」，要往前 251 条：茅台从 2016-01-04 算起的第 252 条行情才算得出来。"""
    universe = rows_of(ds, "600519.SH", D(2016, 1, 4), AS_OF)
    event = f"({YEAR_LINE}) & ~Ref({YEAR_LINE}, 1)"
    result = evaluate(event, "event", universe, D(2016, 1, 1), AS_OF, ds)
    assert result.lookback == 251
    assert result.first_date == universe.sort("date").get_column("date")[251]


def test_新股的EMA从上市当天就算得出来_老股要等满8n条(ds):
    """陕西旅游（603402.SH）2026-01-06 上市，上市以来的行情都在本地；茅台 2016 年前就上市了。"""
    young = rows_of(ds, "603402.SH", D(2026, 1, 6), AS_OF)
    result = evaluate("EMA($close, 26)", "sort", young, D(2026, 1, 6), AS_OF, ds)
    assert result.first_date == D(2026, 1, 6)
    closes = young.join(
        ds.get_fields(["603402.SH"], D(2026, 1, 6), AS_OF, ["close"]), on=["date", "code"]
    )
    expected = (
        closes.sort("date").select(pl.col("close").ewm_mean(span=26, adjust=False)).to_series()
    )
    assert result.values.get_column("value").to_list() == pytest.approx(expected.to_list())

    old = rows_of(ds, "600519.SH", D(2016, 1, 4), D(2017, 6, 30))
    result = evaluate("EMA($close, 26)", "sort", old, D(2016, 1, 4), D(2017, 6, 30), ds)
    assert result.first_date == old.sort("date").get_column("date")[208]
    assert (
        result.values.filter(pl.col("date") < result.first_date).get_column("value").null_count()
        == 208
    )


def test_排名在真实股票池上_最高为1_只给池内有值的股票(ds):
    pool = ds.get_universe_mask(AS_OF, AS_OF, exclude=DEFAULT_EXCLUDE)
    result = evaluate("Rank($amount)", "sort", pool, AS_OF, AS_OF, ds)
    ranks = result.values.get_column("value")
    assert ranks.max() == 1.0
    assert 0 < ranks.min() < 0.001
    assert result.values.height == pool.height


@pytest.mark.skipif(CONCEPT not in _ds.available_targets(), reason="本地概念板块不可用")
def test_概念板块也能算(ds):
    boards = ds.get_fields(None, AS_OF, AS_OF, ["close"], target=CONCEPT).select("date", "code")
    result = evaluate("Sum($amount, 5)", "sort", boards, AS_OF, AS_OF, ds, target=CONCEPT)
    assert result.values.height == boards.height
    assert result.values.get_column("value").null_count() < 5


def test_预热之后一天都算不出来就报错(ds):
    pool = ds.get_universe_mask(D(2016, 1, 4), D(2016, 6, 30))
    with pytest.raises(ExprDataError, match="往前读 250 条"):
        evaluate(YEAR_LINE, "filter", pool, D(2016, 1, 4), D(2016, 6, 30), ds)
