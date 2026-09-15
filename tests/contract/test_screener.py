"""股票表、板块表在本地真实数据上的契约测试。没有本地数据就整个跳过。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from litmus.data import CONCEPT, DataService, MissingDataError
from litmus.expr import evaluate
from litmus.research.screener import SORT_VALUE, run_board_list, run_stock_list
from litmus.spec import parse_spec

AS_OF = date(2026, 9, 11)

_ds = DataService.from_env()
try:
    _first, _last = _ds.data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的股票日频数据", allow_module_level=True)
if _last < AS_OF:
    pytest.skip(f"用例要 {AS_OF} 的数据，本地只到 {_last}", allow_module_level=True)

BREAKOUT = "Cross($close, Mean($close, 250)) & ($amount > Mean(Ref($amount, 1), 20) * 2)"


def stock_list(**fields) -> object:
    spec = parse_spec({"shape": "stock_list", "as_of": AS_OF.isoformat(), **fields})
    return run_stock_list(spec, _ds)


def sort_values(result) -> list[float]:
    return [row[SORT_VALUE] for row in result.rows]


def test_概念板块限定股票池_查询日早于快照日时提示按哪天的成分():
    if CONCEPT not in _ds.available_targets():
        pytest.skip("概念板块不可用")
    snapshot = _ds.concept_snapshot_date()
    board = {"type": "concept", "code": "880728.TDX"}  # 航运概念
    count = len(_ds.board_members("880728.TDX"))

    early = stock_list(as_of="2025-06-03", universe={"board": board}, limit=3)
    assert early.notes[0] == (
        f"「航运概念」按 {snapshot} 的成分（{count} 只）筛选，不是 2025-06-03 当时的成分："
        "之后才调入的股票也算在内，当时在、后来调出的不会出现"
    )
    same_day = stock_list(as_of=snapshot.isoformat(), universe={"board": board}, limit=3)
    assert not any("成分" in note for note in same_day.notes)


def test_放量突破年线_按放量倍数排():
    """和上一步演示的结果一致：满足 13 只，第一名是鼎信通讯（放量 12 倍）。"""
    result = stock_list(
        filter={"expr": BREAKOUT},
        sort={"by": "$amount / Mean(Ref($amount, 1), 20)"},
        limit=5,
    )
    assert result.total == 13
    assert len(result.rows) == 5
    first = result.rows[0]
    assert (first["code"], first["name"]) == ("603421.SH", "鼎信通讯")
    assert first[SORT_VALUE] == pytest.approx(12.21, abs=0.01)
    assert sort_values(result) == sorted(sort_values(result), reverse=True)
    assert result.columns[:4] == ("code", "name", "industry", SORT_VALUE)
    assert {"close_raw", "pct_chg", "amount", "market_cap", "close"} <= set(result.columns)


def test_行业股票池_低市盈率高股息():
    result = stock_list(
        filter={"expr": "($pe_ttm < 6) & ($dv_ttm > 5)"},
        universe={"industry": "银行", "exclude": ["ST", "suspended"]},
        sort={"by": "$dv_ttm"},
    )
    assert result.total == 8
    assert result.rows[0]["code"] == "600015.SH"
    assert all(row["industry"] == "银行" for row in result.rows)


def test_只排序不筛选_整个股票池参与():
    result = stock_list(sort={"by": "$pct_chg"}, limit=5)
    pool = _ds.get_universe_mask(AS_OF, AS_OF, exclude=["ST", "suspended", "new_listing_60d"])
    assert result.total == pool.height
    assert len(result.rows) == 5
    assert sort_values(result) == sorted(sort_values(result), reverse=True)


def test_只筛选不排序_按成交额从高到低():
    result = stock_list(filter={"expr": "$is_limit_up"}, limit=3)
    assert "按成交额从高到低" in result.notes[0]
    amounts = [row["amount"] for row in result.rows]
    assert amounts == sorted(amounts, reverse=True)


def test_排名在整个股票池上算_不是只在筛选结果里排():
    result = stock_list(filter={"expr": "$pct_chg > 9"}, sort={"by": "Rank($amount)"}, limit=500)
    pool = _ds.get_universe_mask(AS_OF, AS_OF, exclude=["ST", "suspended", "new_listing_60d"])
    ranks = evaluate("Rank($amount)", "sort", pool, AS_OF, AS_OF, _ds).values
    expected = dict(ranks.select("code", "value").iter_rows())
    assert result.rows and all(
        row[SORT_VALUE] == pytest.approx(expected[row["code"]]) for row in result.rows
    )
    assert min(sort_values(result)) < 0.9  # 只在涨停股里排的话，最低的名次也会被抬得很高


def test_排序值为空的不进结果():
    result = stock_list(sort={"by": "$pe_ttm", "order": "asc"}, limit=500)
    assert all(row[SORT_VALUE] is not None for row in result.rows)
    assert any("排序值为空" in note for note in result.notes)
    assert sort_values(result) == sorted(sort_values(result))


def test_不是交易日直接报错():
    with pytest.raises(ValueError, match="不是交易日"):
        run_stock_list(parse_spec({"shape": "stock_list", "as_of": "2026-09-06"}), _ds)


@pytest.mark.skipif(CONCEPT not in _ds.available_targets(), reason="本地概念板块不可用")
def test_概念板块表_最近一周成交额前十():
    spec = parse_spec(
        {
            "shape": "board_list",
            "board_type": "concept",
            "as_of": AS_OF.isoformat(),
            "sort": {"by": "Sum($amount, 5)"},
            "limit": 10,
        }
    )
    result = run_board_list(spec, _ds)
    assert len(result.rows) == 10
    assert all(row["name"] for row in result.rows)
    assert result.columns == ("code", "name", SORT_VALUE, "close", "pct_chg", "amount")


def test_申万行业表_今天上涨的行业():
    spec = parse_spec(
        {
            "shape": "board_list",
            "board_type": "sw_industry",
            "as_of": AS_OF.isoformat(),
            "filter": {"expr": "$pct_chg > 0"},
            "sort": {"by": "$pct_chg"},
        }
    )
    result = run_board_list(spec, _ds)
    changes = _ds.get_fields(None, AS_OF, AS_OF, ["pct_chg"], target="sw_industry")
    assert result.total == changes.filter(pl.col("pct_chg") > 0).height
    assert all(row["pct_chg"] > 0 for row in result.rows)
