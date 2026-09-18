"""个股回看在本地真实数据上的契约测试：手工核对几笔交易（第 3 步验收标准）。

每一笔的期望值都直接用行情另算一遍：买入日开盘价、卖出日收盘价、交易日历往后数几天。
没有本地数据就整个跳过。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from litmus.data import DataService, MissingDataError
from litmus.research import Delay, HistoryResult, ListResult, run
from litmus.research.returns import COUNTED, DELISTED, PENDING, UNFILLED
from litmus.spec import parse_spec

D = date
END = D(2026, 9, 11)

_ds = DataService.from_env()
try:
    _first, _last = _ds.data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的股票日频数据", allow_module_level=True)
if _first > D(2016, 1, 4) or _last < END:
    pytest.skip(
        f"用例要 2016-01-04 ~ {END} 的数据，本地只有 {_first} ~ {_last}", allow_module_level=True
    )

CALENDAR = _ds.get_trading_calendar(D(2016, 1, 4), END)
MACD = "Cross(EMA($close,12) - EMA($close,26), EMA(EMA($close,12) - EMA($close,26), 9))"
BREAKOUT = "Cross($close, Mean($close, 250)) & ($amount > Mean(Ref($amount, 1), 20) * 2)"


def history(
    code: str, expr: str, start: date, end: date, horizons=(5, 20, 60), scope=None, **extra
) -> HistoryResult:
    output = {
        "kind": "event_study",
        "event": {"expr": expr, "label": "测试事件"},
        "horizons": list(horizons),
        **extra,
    }
    spec = parse_spec(
        {
            "scope": scope or {},
            "subject": {"kind": "codes", "codes": [code]},
            "when": {"range": {"from": start.isoformat(), "to": end.isoformat()}},
            "output": output,
        }
    )
    return run(spec, _ds)


def record(result: HistoryResult, day: date):
    return next(item for item in result.triggers if item.trigger_date == day)


def shift(day: date, n: int) -> date:
    return CALENDAR[CALENDAR.index(day) + n]


def price(code: str, day: date, column: str) -> float:
    return _ds.get_fields([code], day, day, [column]).row(0, named=True)[column]


def test_涨停后第二天一字板_买入顺延一天():
    """000523.SZ 2025-03-14 涨停，03-17 开盘就涨停买不进，03-18 才买到。"""
    item = record(
        history("000523.SZ", "$is_limit_up", D(2025, 3, 1), D(2025, 3, 31), horizons=[5]),
        D(2025, 3, 14),
    )
    assert item.entry_delay == Delay(1, "涨停")
    assert item.entry_date == shift(D(2025, 3, 14), 2)
    assert item.exit_delay[5] is None
    assert item.exit_date[5] == shift(item.entry_date, 5)
    expected = (
        price("000523.SZ", item.exit_date[5], "close") / price("000523.SZ", item.entry_date, "open")
        - 1
    )
    assert item.returns[5] == pytest.approx(expected)


def test_涨停后长期停牌_无法成交():
    """002411.SZ 2022-04-29 涨停后停牌到 07-01，中间 40 个交易日买不进。"""
    result = history("002411.SZ", "$is_limit_up", D(2022, 4, 1), D(2022, 4, 30), horizons=[5])
    item = record(result, D(2022, 4, 29))
    assert item.status[5] == UNFILLED
    assert item.entry_date is None
    assert "买入顺延超过 20 个交易日（停牌）" in item.notes
    assert result.summary[5].unfilled >= 1
    assert any("无法成交" in note for note in result.notes)


def test_持有期内退市_按最后收盘价结算():
    """000005.SZ 2024-01-29 跌停，2024-04-26 退市：持有 60 天卖不出去，按退市前最后一个交易日收盘价算。"""
    item = record(
        history("000005.SZ", "$is_limit_down", D(2024, 1, 1), D(2024, 1, 31), horizons=[60]),
        D(2024, 1, 29),
    )
    last_day = (
        _ds.get_fields(["000005.SZ"], D(2024, 1, 1), D(2024, 6, 28), ["close"])
        .get_column("date")
        .max()
    )
    assert item.status[60] == DELISTED
    assert item.exit_date[60] == last_day
    expected = (
        price("000005.SZ", last_day, "close") / price("000005.SZ", item.entry_date, "open") - 1
    )
    assert item.returns[60] == pytest.approx(expected)


def test_茅台MACD金叉_统计区间_自身涨跌_同期对照_扣成本():
    result = history("600519.SH", MACD, D(2016, 1, 1), END)
    rows = _ds.get_fields(["600519.SH"], D(2016, 1, 4), END, ["close"])
    assert result.name == "贵州茅台"
    assert result.range == (rows.get_column("date")[282], END)  # MACD 281 条 + 事件包装 1 条
    summary = result.summary[20]
    counted = [item for item in result.triggers if item.status[20] in COUNTED]
    assert summary.n == len(counted) > 20
    assert summary.mean_return_after_cost == pytest.approx(summary.mean_return - 0.003)
    assert summary.mean_baseline_return is not None and 0 <= summary.win_rate <= 1

    item = counted[0]
    buy, sell = item.entry_date, item.exit_date[20]
    assert buy == shift(item.trigger_date, 1)  # 茅台很少停牌，也不会开盘涨停
    assert item.returns[20] == pytest.approx(
        price("600519.SH", sell, "close") / price("600519.SH", buy, "open") - 1
    )

    pool = _ds.get_universe_mask(buy, buy, exclude=["ST", "suspended", "new_listing_60d"])
    codes = pool.get_column("code").to_list()
    opens = _ds.get_fields(codes, buy, buy, ["open"]).select("code", "open")
    closes = _ds.get_fields(codes, sell, sell, ["close"]).select("code", "close")
    both = opens.join(closes, on="code")
    manual = both.select((pl.col("close") / pl.col("open") - 1).mean()).item()
    assert item.market_returns[20] == pytest.approx(manual)
    assert item.market_excluded[20] == opens.height - both.height


def test_同期对照是算的范围的等权平均():
    """牧原在农林牧渔里：对照是买入日农林牧渔（剔除 ST、停牌、次新）的等权平均，不是全A。"""
    result = history(
        "002714.SZ",
        MACD,
        D(2024, 1, 1),
        D(2025, 12, 31),
        horizons=[20],
        scope={"industry": "农林牧渔"},
    )
    item = next(item for item in result.triggers if item.status[20] in COUNTED)
    buy, sell = item.entry_date, item.exit_date[20]
    pool = _ds.get_universe_mask(
        buy, buy, industry="农林牧渔", exclude=["ST", "suspended", "new_listing_60d"]
    )
    codes = pool.get_column("code").to_list()
    opens = _ds.get_fields(codes, buy, buy, ["open"]).select("code", "open")
    closes = _ds.get_fields(codes, sell, sell, ["close"]).select("code", "close")
    manual = opens.join(closes, on="code").select((pl.col("close") / pl.col("open") - 1).mean())
    assert len(codes) < 200  # 用例成立的前提：确实是行业，不是全A
    assert item.market_returns[20] == pytest.approx(manual.item())


def test_换成沪深300对照():
    result = history(
        "600519.SH",
        MACD,
        D(2020, 1, 1),
        D(2021, 12, 31),
        horizons=[20],
        benchmark="index:000300.SH",
    )
    item = next(item for item in result.triggers if item.status[20] in COUNTED)
    index = _ds.get_index_daily("000300.SH", item.entry_date, item.exit_date[20])
    expected = index.get_column("close")[-1] / index.get_column("open")[0] - 1
    assert item.market_returns[20] == pytest.approx(expected)


def test_卖出日在数据末日之后是观察中():
    """鼎信通讯 2026-09-11 放量突破年线，那天就是本地数据末日。"""
    result = history("603421.SH", BREAKOUT, D(2026, 9, 1), END, horizons=[5])
    item = record(result, END)
    assert item.status[5] == PENDING
    assert result.summary[5].pending == 1
    assert any("还没走完" in note for note in result.notes)


def test_入口按形态分派_没解析成代码时直接报错():
    listing = run(
        parse_spec(
            {
                "subject": {"kind": "pool"},
                "when": {"as_of": END.isoformat()},
                "metrics": [{"name": "成交额", "expr": "$amount"}],
                "output": {"kind": "table", "sort": {"by": "成交额"}, "limit": 3},
            }
        ),
        _ds,
    )
    assert isinstance(listing, ListResult) and len(listing.rows) == 3

    spec = parse_spec(
        {
            "subject": {"kind": "codes", "mentions": [{"mention": "茅台"}]},
            "when": {"range": {"from": "2026-01-01", "to": END.isoformat()}},
            "output": {"kind": "event_study", "event": {"expr": "$is_limit_up"}},
        }
    )
    with pytest.raises(ValueError, match="只支持点名一只股票"):
        run(spec, _ds)
