"""表在本地真实数据上的契约测试。没有本地数据就整个跳过。

metrics 算出来的每一列都按 name 取，`sort.by` 填的是其中一个 name。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from litmus.api.checks import check_spec, issue_text
from litmus.data import CONCEPT, DataService, MissingDataError
from litmus.expr import ExprDataError, evaluate
from litmus.research import run
from litmus.research.table import DEFAULT_SORT_NAME
from litmus.signals import load_events

AS_OF = date(2026, 9, 11)

_ds = DataService.from_env()
try:
    _first, _last = _ds.data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的股票日频数据", allow_module_level=True)
if _last < AS_OF:
    pytest.skip(f"用例要 {AS_OF} 的数据，本地只到 {_last}", allow_module_level=True)

BREAKOUT = "Cross($close, Mean($close, 250)) & ($amount > Mean(Ref($amount, 1), 20) * 2)"


#: 排序用的那一列叫什么，各个用例自己指定
SORT = "排序值"


_EVENTS = load_events()


def table(*, metrics=None, scope=None, as_of="", **output) -> object:
    """一张表。走 check_spec 而不是 parse_spec：日期回填、补默认排序都在那里。"""
    spec, issues = check_spec(
        {
            "scope": scope or {},
            "subject": {"kind": "pool"},
            "when": {"as_of": as_of or AS_OF.isoformat()},
            "metrics": metrics if metrics is not None else [],
            "output": {"kind": "table", **output},
        },
        _ds,
        _EVENTS,
    )
    assert spec is not None, [issue_text(i) for i in issues]
    return run(spec, _ds)


def by(expr: str, name: str = SORT) -> dict:
    return {"name": name, "expr": expr}


def sort_values(result, name: str = SORT) -> list[float]:
    return [row[name] for row in result.rows]


def column_names(result) -> tuple[str, ...]:
    return (*result.head, *(c.name for c in result.columns))


def test_从某天起的涨幅按日期取值_停过牌的股票不再算偏():
    """2026-09-15 实测：Pct($close, 170) 数条数，江丰电子、有研硅今年停过牌，数到去年更早的日子
    （江丰电子 +145.5%，按日期是 +158.2%）。PctSince 和 2025-12-31 的收盘价比，用 Polars 另算一遍对照。"""
    day, base = date(2026, 9, 14), date(2025, 12, 31)
    if _last < day:
        pytest.skip(f"用例要 {day} 的数据，本地只到 {_last}")
    codes = [_ds.resolve_stock(name)[0].code for name in ("江丰电子", "有研硅")]
    universe = _ds.get_universe_mask(day, day).filter(pl.col("code").is_in(codes))
    result = evaluate("PctSince($close, 20251231)", "sort", universe, day, day, _ds)
    got = dict(result.values.select("code", "value").rows())

    closes = _ds.get_fields(codes, date(2025, 11, 3), day, ["close"])
    for code in codes:
        rows = closes.filter(pl.col("code") == code)
        start = rows.filter(pl.col("date") <= base).get_column("close")[-1]
        end = rows.filter(pl.col("date") == day).get_column("close")[0]
        assert got[code] == pytest.approx(end / start - 1)

    with pytest.raises(ExprDataError, match="要早于"):
        evaluate("PctSince($close, 20260914)", "sort", universe, day, day, _ds)


def test_概念板块限定股票池_查询日早于快照日时提示按哪天的成分():
    if CONCEPT not in _ds.available_targets():
        pytest.skip("概念板块不可用")
    snapshot = _ds.concept_snapshot_date()
    board = {"type": "concept", "code": "880728.TDX"}  # 航运概念
    count = len(_ds.board_members("880728.TDX"))

    early = table(
        as_of="2025-06-03",
        scope={"board": board},
        metrics=[by("$amount")],
        sort={"by": SORT},
        limit=3,
    )
    assert early.notes[0] == (
        f"「航运概念」按 {snapshot} 的成分（{count} 只）筛选，不是 2025-06-03 当时的成分："
        "之后才调入的股票也算在内，当时在、后来调出的不会出现"
    )
    same_day = table(
        as_of=snapshot.isoformat(),
        scope={"board": board},
        metrics=[by("$amount")],
        sort={"by": SORT},
        limit=3,
    )
    assert not any("成分" in note for note in same_day.notes)


def test_放量突破年线_按放量倍数排():
    """和上一步演示的结果一致：满足 13 只，第一名是鼎信通讯（放量 12 倍）。"""
    result = table(
        metrics=[by("$amount / Mean(Ref($amount, 1), 20)", "放量倍数"), by("$close", "收盘价")],
        filter={"expr": BREAKOUT},
        sort={"by": "放量倍数"},
        limit=5,
    )
    assert result.total == 13
    assert len(result.rows) == 5
    first = result.rows[0]
    assert (first["code"], first["name"]) == ("603421.SH", "鼎信通讯")
    assert first["放量倍数"] == pytest.approx(12.21, abs=0.01)
    assert sort_values(result, "放量倍数") == sorted(sort_values(result, "放量倍数"), reverse=True)
    # 列 = 代码名称行业 + metrics，metrics 里写什么就有什么，不多也不少
    assert column_names(result) == ("code", "name", "industry", "放量倍数", "收盘价")


def test_行业股票池_低市盈率高股息():
    result = table(
        metrics=[by("$dv_ttm")],
        scope={"industry": "银行", "exclude": ["ST", "suspended"]},
        filter={"expr": "($pe_ttm < 6) & ($dv_ttm > 5)"},
        sort={"by": SORT},
    )
    assert result.total == 8
    assert result.rows[0]["code"] == "600015.SH"
    assert all(row["industry"] == "银行" for row in result.rows)


def test_只排序不筛选_整个股票池参与():
    result = table(metrics=[by("$pct_chg")], sort={"by": SORT}, limit=5)
    pool = _ds.get_universe_mask(AS_OF, AS_OF, exclude=["ST", "suspended", "new_listing_60d"])
    assert result.total == pool.height
    assert len(result.rows) == 5
    assert sort_values(result) == sorted(sort_values(result), reverse=True)


def test_只筛选不排序_代码补一个成交额指标并按它排():
    result = table(filter={"expr": "$is_limit_up"}, limit=3)
    # 补出来的指标会显示在结果里，确认卡上也标成默认值
    assert DEFAULT_SORT_NAME in column_names(result)
    amounts = sort_values(result, DEFAULT_SORT_NAME)
    assert amounts == sorted(amounts, reverse=True)


def test_排名在整个股票池上算_不是只在筛选结果里排():
    result = table(
        metrics=[by("Rank($amount)")],
        filter={"expr": "$pct_chg > 9"},
        sort={"by": SORT},
        limit=500,
    )
    pool = _ds.get_universe_mask(AS_OF, AS_OF, exclude=["ST", "suspended", "new_listing_60d"])
    ranks = evaluate("Rank($amount)", "sort", pool, AS_OF, AS_OF, _ds).values
    expected = dict(ranks.select("code", "value").iter_rows())
    assert result.rows and all(
        row[SORT] == pytest.approx(expected[row["code"]]) for row in result.rows
    )
    assert min(sort_values(result)) < 0.9  # 只在涨停股里排的话，最低的名次也会被抬得很高


def test_排序值为空的不进结果_说清为什么():
    """QUESTIONS.md 翻车模式 #4：以前只说「有 N 只排序值为空」，用户会以为系统没找到。"""
    result = table(metrics=[by("$pe_ttm")], sort={"by": SORT, "order": "asc"}, limit=500)
    assert all(row[SORT] is not None for row in result.rows)
    assert sort_values(result) == sorted(sort_values(result))
    note = next(note for note in result.notes if "排序值为空" in note)
    pool = _ds.get_universe_mask(AS_OF, AS_OF, exclude=["ST", "suspended", "new_listing_60d"])
    pe = _ds.get_fields(pool.get_column("code").to_list(), AS_OF, AS_OF, ["pe_ttm"])
    empty = pe.get_column("pe_ttm").null_count()
    assert (
        note
        == f"有 {empty} 只排序值为空，没有参与排序：{empty} 只当天没有市盈率TTM（亏损股没有市盈率）"
    )


def test_行情不够长的也说出来():
    result = table(metrics=[by("Mean($close, 250)")], sort={"by": SORT}, limit=500)
    note = next(note for note in result.notes if "排序值为空" in note)
    assert "行情不到 250 个交易日，算不出来" in note


def test_不是交易日在检查这一步就拦下():
    spec, issues = check_spec(
        {"subject": {"kind": "pool"}, "when": {"as_of": "2026-09-06"}, "output": {"kind": "table"}},
        _ds,
        _EVENTS,
    )
    assert spec is None
    assert any("不是交易日" in issue_text(i) for i in issues)


@pytest.mark.skipif(CONCEPT not in _ds.available_targets(), reason="本地概念板块不可用")
def test_概念板块表_最近一周成交额前十():
    result = table(
        scope={"target": "concept"},
        metrics=[by("Sum($amount, 5)", "5日成交额")],
        sort={"by": "5日成交额"},
        limit=10,
    )
    assert len(result.rows) == 10
    assert all(row["name"] for row in result.rows)
    assert column_names(result) == ("code", "name", "5日成交额")  # 板块没有行业列


def test_申万行业表_今天上涨的行业():
    result = table(
        scope={"target": "sw_industry"},
        metrics=[by("$pct_chg", "涨跌幅")],
        filter={"expr": "$pct_chg > 0"},
        sort={"by": "涨跌幅"},
    )
    changes = _ds.get_fields(None, AS_OF, AS_OF, ["pct_chg"], target="sw_industry")
    assert result.total == changes.filter(pl.col("pct_chg") > 0).height
    assert all(row["涨跌幅"] > 0 for row in result.rows)
    assert result.pool_size == changes.height
