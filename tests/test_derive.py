"""读取时现算字段的小表格测试：财务按公告日对齐、公告日顺延、除权除息日、次新股。不读文件。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from litmus.data.derive import (
    EX_DIV_MIN_CHANGE,
    AmbiguousDataError,
    finance_timeline,
    with_event,
    with_ex_div,
    with_finance,
    with_is_new,
)

D = date


def rows(code: str, *days: date, adj: tuple[float, ...] | None = None) -> pl.DataFrame:
    data = {"code": [code] * len(days), "date": list(days)}
    schema = {"code": pl.String, "date": pl.Date}
    if adj is not None:
        data["adj_factor"] = list(adj)
        schema["adj_factor"] = pl.Float64
    return pl.DataFrame(data, schema=schema)


# ── 财务：按公告日对齐 ──────────────────────────────────────────


def fina(*records: tuple) -> pl.DataFrame:
    """每条记录：(股票, 公告日, 报告期, 年化 ROE, update_flag)。"""
    return pl.DataFrame(
        [
            {
                "code": code,
                "ann_date": ann,
                "period": period,
                "roe": None,
                "roe_yearly": value,
                "revenue_yoy": None,
                "profit_yoy": None,
                "update_flag": flag,
            }
            for code, ann, period, value, flag in records
        ],
        schema={
            "code": pl.String,
            "ann_date": pl.Date,
            "period": pl.Date,
            "roe": pl.Float64,
            "roe_yearly": pl.Float64,
            "revenue_yoy": pl.Float64,
            "profit_yoy": pl.Float64,
            "update_flag": pl.String,
        },
    )


def roe_on(table: pl.DataFrame, *days: date, code: str = "A") -> list[float | None]:
    out = with_finance(rows(code, *days), finance_timeline(table)).sort("date")
    return out.get_column("roe").to_list()


Q1, H1, Y25 = D(2026, 3, 31), D(2026, 6, 30), D(2025, 12, 31)


def test_公告之前没有值_公告当天起生效_roe取年化口径():
    table = fina(("A", D(2026, 4, 28), Q1, 10.0, "1"))
    assert roe_on(table, D(2026, 4, 27), D(2026, 4, 28), D(2026, 4, 29)) == [None, 10.0, 10.0]


def test_周末公告从下一个交易日起生效():
    table = fina(("A", D(2026, 8, 29), H1, 10.0, "1"))  # 周六
    assert roe_on(table, D(2026, 8, 28), D(2026, 8, 31)) == [None, 10.0]


def test_同一天公告两期取报告期大的():
    table = fina(("A", D(2026, 4, 28), Y25, 8.0, "1"), ("A", D(2026, 4, 28), Q1, 10.0, "1"))
    assert roe_on(table, D(2026, 4, 28)) == [10.0]


def test_更正之后取更正的值():
    table = fina(("A", D(2026, 4, 22), Q1, 8.19, "1"), ("A", D(2026, 4, 29), Q1, 8.18, "1"))
    assert roe_on(table, D(2026, 4, 28), D(2026, 4, 29)) == [8.19, 8.18]


def test_旧报告期的更正不覆盖更新的一期():
    """实测 001299.SZ：一季报在半年报公告之后才更正，那天起取的仍是半年报。"""
    table = fina(
        ("A", D(2026, 4, 28), Q1, 9.02, "1"),
        ("A", D(2026, 8, 20), H1, 12.0, "1"),
        ("A", D(2026, 8, 27), Q1, 9.10, "1"),
    )
    assert roe_on(table, D(2026, 5, 6), D(2026, 8, 21), D(2026, 8, 27)) == [9.02, 12.0, 12.0]


def test_补发的旧报告期不让数值倒退():
    table = fina(("A", D(2026, 4, 28), Q1, 5.0, "1"), ("A", D(2026, 5, 10), Y25, 3.0, "1"))
    assert roe_on(table, D(2026, 5, 11)) == [5.0]


def test_同一天新旧两个版本取标1的():
    """实测 002122.SZ 2023 年报：同一公告日 6.4353（标 0）与 6.4077（标 1）。"""
    table = fina(("A", D(2024, 4, 19), Y25, 6.4353, "0"), ("A", D(2024, 4, 19), Y25, 6.4077, "1"))
    assert roe_on(table, D(2024, 4, 19)) == [6.4077]


def test_旧版本在新版本出现之前照常有效():
    table = fina(("A", D(2026, 4, 22), Q1, 8.0, "0"), ("A", D(2026, 4, 29), Q1, 8.1, "1"))
    assert roe_on(table, D(2026, 4, 23), D(2026, 4, 29)) == [8.0, 8.1]


def test_新版本之后才冒出来的旧版本不算():
    """实测 26 组：旧版本的公告日比新版本还晚一天。"""
    table = fina(("A", D(2026, 8, 24), H1, 7.0, "1"), ("A", D(2026, 8, 25), H1, 7.5, "0"))
    assert roe_on(table, D(2026, 8, 25)) == [7.0]


def test_只有旧版本的照常用():
    table = fina(("A", D(2026, 4, 28), Q1, 6.0, "0"))
    assert roe_on(table, D(2026, 4, 28)) == [6.0]


def test_股票之间互不影响():
    table = fina(("A", D(2026, 4, 28), Q1, 6.0, "1"), ("B", D(2026, 4, 20), Q1, 9.0, "1"))
    assert roe_on(table, D(2026, 4, 27), code="A") == [None]
    assert roe_on(table, D(2026, 4, 27), code="B") == [9.0]


def test_同一天两个最新版本分不出来就报错():
    table = fina(("A", D(2026, 4, 28), Q1, 6.0, "1"), ("A", D(2026, 4, 28), Q1, 6.1, "1"))
    with pytest.raises(AmbiguousDataError, match="分不出"):
        finance_timeline(table)


def test_update_flag出现没见过的取值就报错():
    with pytest.raises(AmbiguousDataError, match="update_flag"):
        finance_timeline(fina(("A", D(2026, 4, 28), Q1, 6.0, "2")))


# ── 公告日事件 ──────────────────────────────────────────────────


LISTED = pl.DataFrame({"code": ["A"], "list_date": [D(2010, 1, 4)]})
SINCE = D(2016, 1, 1)


def flagged(
    trading: pl.DataFrame,
    *announced: date,
    list_dates: pl.DataFrame = LISTED,
    since: date = SINCE,
) -> list[date]:
    events = pl.DataFrame({"code": ["A"] * len(announced), "date": list(announced)})
    out = with_event(trading, events, "hit", list_dates=list_dates, since=since)
    assert out.height == trading.height  # 只加一列，不增减行
    return sorted(out.filter(pl.col("hit")).get_column("date").to_list())


def test_公告当天有行情就标在当天():
    assert flagged(rows("A", D(2026, 8, 27), D(2026, 8, 28)), D(2026, 8, 28)) == [D(2026, 8, 28)]


def test_周末公告顺延到下一个交易日():
    assert flagged(rows("A", D(2026, 8, 28), D(2026, 8, 31)), D(2026, 8, 29)) == [D(2026, 8, 31)]


def test_停牌期间的公告顺延到复牌那天():
    assert flagged(rows("A", D(2026, 3, 2), D(2026, 3, 23)), D(2026, 3, 10)) == [D(2026, 3, 23)]


def test_更早的公告落在起点之前的那一行上_不会被顺延进来():
    """调用方带上起点之前的最后一行（2/27），2/26 的公告落在它上面，截掉之后起点那天不标。"""
    trading = rows("A", D(2026, 2, 27), D(2026, 3, 23))
    assert flagged(trading, D(2026, 2, 26)) == [D(2026, 2, 27)]


def test_同一天落了两个公告只标一次():
    assert flagged(rows("A", D(2026, 8, 31)), D(2026, 8, 29), D(2026, 8, 30)) == [D(2026, 8, 31)]


def test_上市之前的公告不标():
    listed = pl.DataFrame({"code": ["A"], "list_date": [D(2026, 1, 6)]})
    trading = rows("A", D(2026, 1, 6), D(2026, 1, 7))
    assert flagged(trading, D(2025, 12, 20), list_dates=listed) == []


def test_查不到上市日的照常标():
    no_listing = pl.DataFrame(schema={"code": pl.String, "list_date": pl.Date})
    assert flagged(rows("A", D(2026, 8, 31)), D(2026, 8, 29), list_dates=no_listing) == [
        D(2026, 8, 31)
    ]


def test_早于本地行情起点的公告不标():
    trading = rows("A", D(2016, 1, 4))
    assert flagged(trading, D(2015, 12, 31)) == []
    assert flagged(trading, D(2016, 1, 2)) == [D(2016, 1, 4)]


def test_最后一条行情之后的公告不标():
    assert flagged(rows("A", D(2026, 8, 28)), D(2026, 9, 1)) == []


# ── 除权除息日 ──────────────────────────────────────────────────


def ex_div(trading: pl.DataFrame) -> list[bool]:
    return with_ex_div(trading).sort("code", "date").get_column("is_ex_div").to_list()


WEEK = [D(2026, 6, 22) + timedelta(days=i) for i in range(5)]


def test_复权因子涨过门槛才算除权():
    bump = 1 + EX_DIV_MIN_CHANGE * 2
    assert ex_div(rows("A", *WEEK[:3], adj=(1.0, 1.0, bump))) == [False, False, True]


def test_舍入抖动不算除权():
    """实测 000001.SZ：125.0496 → 125.0493 → 125.049，是小数位数时三时四造成的。"""
    assert ex_div(rows("A", *WEEK[:3], adj=(125.0496, 125.0493, 125.049))) == [False] * 3


def test_复权因子变小不算除权():
    assert ex_div(rows("A", *WEEK[:2], adj=(2.0, 1.5))) == [False, False]


def test_停牌期间除权标在复牌那天():
    trading = rows("A", D(2026, 6, 1), D(2026, 7, 20), adj=(1.0, 1.3))
    assert ex_div(trading) == [False, True]


def test_每只股票第一行不标_股票之间不串():
    trading = pl.concat([rows("A", WEEK[0], adj=(1.0,)), rows("B", WEEK[1], adj=(3.0,))])
    assert ex_div(trading) == [False, False]


# ── 次新股 ──────────────────────────────────────────────────────


CALENDAR = [D(2026, 1, 5) + timedelta(days=i) for i in range(12) if (5 + i) % 7 not in (3, 4)]


def is_new(trading: pl.DataFrame, list_date: date | None, days: int = 3) -> list[bool]:
    listing = pl.DataFrame(
        {"code": ["A"], "list_date": [list_date]}, schema={"code": pl.String, "list_date": pl.Date}
    )
    return with_is_new(trading, listing, CALENDAR, days).sort("date").get_column("is_new").to_list()


def test_日历是工作日():
    assert CALENDAR[0] == D(2026, 1, 5) and D(2026, 1, 10) not in CALENDAR


def test_上市当天算第一天_前N个交易日都算次新():
    trading = rows("A", D(2026, 1, 6), D(2026, 1, 7), D(2026, 1, 8), D(2026, 1, 9))
    assert is_new(trading, D(2026, 1, 6)) == [True, True, True, False]


def test_停牌的日子也算上市的日子():
    trading = rows("A", D(2026, 1, 6), D(2026, 1, 12))
    assert is_new(trading, D(2026, 1, 6)) == [True, False]


def test_非交易日上市从下一个交易日算第一天():
    trading = rows("A", D(2026, 1, 12), D(2026, 1, 13), D(2026, 1, 14), D(2026, 1, 15))
    assert is_new(trading, D(2026, 1, 10)) == [True, True, True, False]


def test_上市早于交易日历起点的不算次新():
    assert is_new(rows("A", D(2026, 1, 5)), D(2025, 12, 30)) == [False]


def test_查不到上市日的不算次新():
    assert is_new(rows("A", D(2026, 1, 5)), None) == [False]
