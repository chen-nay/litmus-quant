"""收益计算的小表格测试：买卖日期、顺延、退市、观察中、同期市场平均。不读数据。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from litmus.research.returns import (
    DELISTED,
    DELISTED_NOTE,
    DONE,
    MISSING_LIMIT_NOTE,
    PENDING,
    UNFILLED,
    Delay,
    Tape,
    pool_average,
)

#: 40 个连续工作日当交易日历
CALENDAR = [
    day for day in (date(2026, 1, 5) + timedelta(days=i) for i in range(60)) if day.weekday() < 5
][:40]
D = CALENDAR  # D[0] 是第一个交易日


def tape(
    *,
    suspended: set[int] = frozenset(),
    open_up: set[int] = frozenset(),
    down: set[int] = frozenset(),
    missing: set[int] = frozenset(),
    until: int | None = None,
    delist: bool = False,
) -> Tape:
    """开盘价 = 10 + i，收盘价 = 10.5 + i；suspended 那几天没有行，until 之后都没有行。"""
    last = len(CALENDAR) - 1 if until is None else until
    rows = [
        {
            "date": CALENDAR[i],
            "open": 10.0 + i,
            "close": 10.5 + i,
            "open_limit_up": None if i in missing else i in open_up,
            "is_limit_down": None if i in missing else i in down,
        }
        for i in range(last + 1)
        if i not in suspended
    ]
    frame = pl.DataFrame(
        rows,
        schema={
            "date": pl.Date,
            "open": pl.Float64,
            "close": pl.Float64,
            "open_limit_up": pl.Boolean,
            "is_limit_down": pl.Boolean,
        },
    )
    return Tape(CALENDAR, frame, delist_date=CALENDAR[last] + timedelta(days=1) if delist else None)


def expected_return(buy: int, sell: int) -> float:
    return (10.5 + sell) / (10.0 + buy) - 1


# ── 正常买卖 ────────────────────────────────────────────────────


def test_次日开盘买入_持有N天后收盘卖出():
    trade = tape().trade(D[0], 5)
    assert (trade.status, trade.entry_date, trade.exit_date) == (DONE, D[1], D[6])
    assert trade.entry_delay is None and trade.exit_delay is None
    assert trade.ret == pytest.approx(expected_return(1, 6))


# ── 顺延 ────────────────────────────────────────────────────────


def test_次日开盘涨停_买入顺延():
    trade = tape(open_up={1}).trade(D[0], 5)
    assert trade.entry_date == D[2]
    assert trade.entry_delay == Delay(1, "涨停")


def test_持有期从实际买入日起算():
    """顺延 3 天后照样持有完整的 5 天：卖出日是买入日之后第 5 个交易日，不是触发日之后第 6 个。"""
    trade = tape(open_up={1, 2}, suspended={3}).trade(D[0], 5)
    assert trade.entry_date == D[4]
    assert trade.entry_delay == Delay(3, "涨停、停牌")
    assert trade.exit_date == D[9]
    assert trade.ret == pytest.approx(expected_return(4, 9))


def test_卖出日跌停或停牌_卖出顺延():
    trade = tape(down={6}, suspended={7}).trade(D[0], 5)
    assert trade.exit_date == D[8]
    assert trade.exit_delay == Delay(2, "跌停、停牌")
    assert trade.ret == pytest.approx(expected_return(1, 8))


def test_开盘涨停不影响卖出_跌停不影响买入():
    trade = tape(down={1}, open_up={6}).trade(D[0], 5)
    assert (trade.entry_date, trade.exit_date) == (D[1], D[6])


def test_顺延正好20天还算成交_超过20天无法成交():
    ok = tape(suspended=set(range(1, 21))).trade(D[0], 5)
    assert ok.status == DONE and ok.entry_delay == Delay(20, "停牌")

    stuck = tape(suspended=set(range(1, 22))).trade(D[0], 5)
    assert stuck.status == UNFILLED
    assert "买入顺延超过 20 个交易日（停牌）" in stuck.notes

    stuck_sell = tape(suspended=set(range(6, 27))).trade(D[0], 5)
    assert stuck_sell.status == UNFILLED and stuck_sell.entry_date == D[1]
    assert "卖出顺延超过 20 个交易日（停牌）" in stuck_sell.notes


# ── 观察中 ──────────────────────────────────────────────────────


def test_卖出日落在数据末日之后是观察中():
    trade = tape().trade(D[35], 5)
    assert (trade.status, trade.entry_date, trade.exit_date) == (PENDING, D[36], None)
    assert trade.ret is None


def test_触发日就是数据末日是观察中():
    assert tape().trade(D[39], 5).status == PENDING


def test_停牌到数据末日还不满20天是观察中而不是无法成交():
    trade = tape(suspended=set(range(26, 40))).trade(D[25], 5)
    assert trade.status == PENDING


# ── 退市 ────────────────────────────────────────────────────────


def test_持有期内退市按最后收盘价结算():
    trade = tape(until=4, delist=True).trade(D[0], 5)
    assert (trade.status, trade.entry_date, trade.exit_date) == (DELISTED, D[1], D[4])
    assert trade.ret == pytest.approx(expected_return(1, 4))
    assert DELISTED_NOTE in trade.notes


def test_退市前连续跌停卖不出去_也按最后收盘价结算():
    trade = tape(until=10, delist=True, down={6, 7, 8, 9, 10}).trade(D[0], 5)
    assert (trade.status, trade.exit_date) == (DELISTED, D[10])


def test_退市前还卖得出去就正常卖():
    trade = tape(until=10, delist=True).trade(D[0], 5)
    assert (trade.status, trade.exit_date) == (DONE, D[6])


def test_触发之后已经退市买不进():
    assert tape(until=0, delist=True).trade(D[0], 5).status == UNFILLED


def test_没退市的股票长期停牌到数据末日不按退市算():
    trade = tape(until=4).trade(D[0], 5)
    assert trade.status == UNFILLED  # 停牌超过 20 天


# ── 其他 ────────────────────────────────────────────────────────


def test_缺涨跌停价按可以成交处理_备注写明():
    trade = tape(missing={1, 6}).trade(D[0], 5)
    assert (trade.status, trade.entry_date, trade.exit_date) == (DONE, D[1], D[6])
    assert trade.notes == (MISSING_LIMIT_NOTE,)


def test_触发日不在交易日历里直接报错():
    with pytest.raises(ValueError, match="不在交易日历里"):
        tape().trade(date(2026, 1, 10), 5)  # 周六


def test_有行情的交易日():
    assert tape(suspended={2, 3}, until=5).trade_days() == [D[0], D[1], D[4], D[5]]


# ── 同期市场平均 ────────────────────────────────────────────────


def test_同期市场平均_停牌剔除_退市按最后价格_不在池内的不算():
    buy, sell = D[1], D[6]
    windows = pl.DataFrame({"window": [0], "buy_date": [buy], "sell_date": [sell]})
    pool = pl.DataFrame({"date": [buy] * 3, "code": ["A", "B", "C"]})
    prices = pl.DataFrame(
        {
            "date": [buy, buy, buy, buy, sell, sell, D[3]],
            "code": ["A", "B", "C", "X", "A", "X", "C"],
            "open": [10.0, 20.0, 30.0, 5.0, 11.0, 6.0, 15.0],
            "close": [10.0, 20.0, 30.0, 5.0, 11.0, 6.0, 15.0],
        }
    )
    delisted = pl.DataFrame({"code": ["C"], "last_date": [D[3]], "last_close": [15.0]})
    row = pool_average(windows, pool, prices, delisted).row(0, named=True)
    # A 涨 10%；B 卖出日停牌剔除；C 持有期内退市按 15 算，跌 50%；X 不在池内
    assert row["market_return"] == pytest.approx((0.10 - 0.50) / 2)
    assert (row["used"], row["excluded"]) == (2, 1)


def test_同期市场平均_多个窗口各算各的():
    windows = pl.DataFrame({"window": [0, 1], "buy_date": [D[1], D[2]], "sell_date": [D[3], D[4]]})
    pool = pl.DataFrame({"date": [D[1], D[2]], "code": ["A", "A"]})
    prices = pl.DataFrame(
        {
            "date": D[1:5],
            "code": ["A"] * 4,
            "open": [10.0, 20.0, 30.0, 40.0],
            "close": [10.0, 20.0, 30.0, 40.0],
        }
    )
    empty = pl.DataFrame(schema={"code": pl.String, "last_date": pl.Date, "last_close": pl.Float64})
    result = pool_average(windows, pool, prices, empty)
    assert result.get_column("market_return").to_list() == pytest.approx([2.0, 1.0])
