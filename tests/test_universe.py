"""按日股票池的小表格测试：剔除项、指数成分快照、申万归属、组合条件。不读文件。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from litmus.data.derive import AmbiguousDataError
from litmus.data.storage import MissingDataError
from litmus.data.universe import (
    index_members,
    industry_members,
    parse_exclude,
    universe_mask,
)

D = date
HS300 = "000300.SH"


def traded(*records: tuple) -> pl.DataFrame:
    """每条：(日期, 代码, 是否 ST)。"""
    return pl.DataFrame(
        [{"date": day, "code": code, "is_st": st} for day, code, st in records],
        schema={"date": pl.Date, "code": pl.String, "is_st": pl.Boolean},
    )


def pairs(mask: pl.DataFrame) -> list[tuple[date, str]]:
    return mask.select("date", "code").rows()


def weights(*records: tuple) -> pl.DataFrame:
    """每条：(快照日, 代码)，都是沪深300。"""
    return pl.DataFrame(
        [{"index_code": HS300, "code": code, "date": day} for day, code in records],
        schema={"index_code": pl.String, "code": pl.String, "date": pl.Date},
    )


def members(*records: tuple) -> pl.DataFrame:
    """每条：(代码, 行业代码, 纳入日, 剔除日)。"""
    return pl.DataFrame(
        [
            {"code": c, "industry_code": i, "industry_name": i, "in_date": a, "out_date": b}
            for c, i, a, b in records
        ],
        schema={
            "code": pl.String,
            "industry_code": pl.String,
            "industry_name": pl.String,
            "in_date": pl.Date,
            "out_date": pl.Date,
        },
    )


# ── 剔除项 ──────────────────────────────────────────────────────


def test_剔除项解析():
    assert parse_exclude(["ST", "suspended", "new_listing_60d"]) == (True, 60)
    assert parse_exclude([]) == (False, None)


def test_不认识的剔除项直接报错():
    with pytest.raises(ValueError, match="new_listing_<N>d"):
        parse_exclude(["新股"])


def test_北交所不进池():
    mask = universe_mask(traded((D(2026, 9, 1), "920001.BJ", False), (D(2026, 9, 1), "A", False)))
    assert pairs(mask) == [(D(2026, 9, 1), "A")]


def test_ST按当天状态剔除():
    data = traded((D(2025, 4, 29), "A", False), (D(2025, 4, 30), "A", True))
    assert pairs(universe_mask(data, exclude=["ST"])) == [(D(2025, 4, 29), "A")]
    assert len(pairs(universe_mask(data))) == 2  # 不剔除 ST 时都在


def test_停牌的日子不在池内():
    """池子只从当天有行情的股票里选：B 在 9/2 没有行情，就不在那天的池里。"""
    data = traded(
        (D(2026, 9, 1), "A", False), (D(2026, 9, 1), "B", False), (D(2026, 9, 2), "A", False)
    )
    assert pairs(universe_mask(data, exclude=["suspended"])) == [
        (D(2026, 9, 1), "A"),
        (D(2026, 9, 1), "B"),
        (D(2026, 9, 2), "A"),
    ]


def test_剔除上市不满N个交易日的次新股():
    calendar = [D(2026, 1, 5), D(2026, 1, 6), D(2026, 1, 7)]
    listing = pl.DataFrame({"code": ["A"], "list_date": [D(2026, 1, 5)]})
    data = traded(*((day, "A", False) for day in calendar))
    mask = universe_mask(data, exclude=["new_listing_2d"], list_dates=listing, calendar=calendar)
    assert pairs(mask) == [(D(2026, 1, 7), "A")]


# ── 指数成分 ────────────────────────────────────────────────────


def test_用当天及之前最近一期快照():
    snapshots = weights(
        (D(2026, 1, 29), "A"), (D(2026, 1, 29), "B"), (D(2026, 2, 26), "B"), (D(2026, 2, 26), "C")
    )
    days = pl.DataFrame({"date": [D(2026, 2, 2), D(2026, 2, 26)]})
    assert sorted(index_members(days, snapshots, HS300).rows()) == [
        (D(2026, 2, 2), "A"),
        (D(2026, 2, 2), "B"),
        (D(2026, 2, 26), "B"),
        (D(2026, 2, 26), "C"),
    ]


def test_早于第一期快照直接报错而不是给空池子():
    snapshots = weights((D(2016, 1, 29), "A"))
    days = pl.DataFrame({"date": [D(2016, 1, 15)]})
    with pytest.raises(MissingDataError, match="2016-01-29"):
        index_members(days, snapshots, HS300)


# ── 申万归属 ────────────────────────────────────────────────────


def industry_on(sw: pl.DataFrame, day: date, industry: str, *codes: str) -> list[str]:
    pool = pl.DataFrame({"date": [day] * len(codes), "code": list(codes)})
    return sorted(industry_members(pool, sw, industry).get_column("code").to_list())


def test_剔除日当天还算旧行业_第二天算新行业():
    """实测 000159.SZ：建筑装饰 7/29 剔除，电力设备 7/30 纳入。"""
    sw = members(("A", "建筑", D(2022, 7, 29), D(2024, 7, 29)), ("A", "电力", D(2024, 7, 30), None))
    assert industry_on(sw, D(2024, 7, 29), "建筑", "A") == ["A"]
    assert industry_on(sw, D(2024, 7, 29), "电力", "A") == []
    assert industry_on(sw, D(2024, 7, 30), "电力", "A") == ["A"]


def test_旧归属没关闭时取纳入日最新的那条():
    """实测 000595.SZ：机械设备 1996 起一直没关，公用事业 2026-07-01 起。"""
    sw = members(
        ("A", "机械", D(1996, 4, 19), D(2026, 6, 30)),
        ("A", "机械", D(1996, 4, 19), None),
        ("A", "公用", D(2026, 7, 1), None),
    )
    assert industry_on(sw, D(2026, 6, 30), "机械", "A") == ["A"]
    assert industry_on(sw, D(2026, 7, 1), "机械", "A") == []
    assert industry_on(sw, D(2026, 7, 1), "公用", "A") == ["A"]


def test_同一天纳入两个行业分不出来就报错():
    sw = members(("A", "传媒", D(1996, 6, 28), None), ("A", "石油", D(1996, 6, 28), None))
    with pytest.raises(AmbiguousDataError, match="分不出"):
        industry_on(sw, D(2026, 9, 1), "传媒", "A")


def test_其他行业的股票不进来():
    sw = members(("A", "银行", D(2000, 1, 1), None), ("B", "电子", D(2000, 1, 1), None))
    assert industry_on(sw, D(2026, 9, 1), "银行", "A", "B") == ["A"]


# ── 组合 ────────────────────────────────────────────────────────


def test_指数_行业_板块取交集():
    day = D(2026, 9, 1)
    data = traded(*((day, code, False) for code in ("A", "B", "C", "D")))
    mask = universe_mask(
        data,
        base="hs300",
        weights=weights((D(2026, 8, 31), "A"), (D(2026, 8, 31), "B"), (D(2026, 8, 31), "C")),
        industry_code="银行",
        sw_member=members(*((code, "银行", D(2000, 1, 1), None) for code in ("A", "B", "D"))),
        board_codes=["B", "C", "D"],
    )
    assert pairs(mask) == [(day, "B")]


def test_不认识的股票池直接报错():
    with pytest.raises(ValueError, match="sz50"):
        universe_mask(traded(), base="sz50")
