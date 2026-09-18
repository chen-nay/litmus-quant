"""卡在本地真实数据上的契约测试（第 5 步验收：A101、A201）。没有本地数据就整个跳过。

数都在这里另算一遍核对：分位、区间用 `evaluate` 单独算，排名按整个算的范围排序数出来。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from litmus.api.checks import check_spec, issue_text
from litmus.data import DataService, MissingDataError
from litmus.expr import evaluate
from litmus.research import run_card
from litmus.signals import load_events
from litmus.spec import DEFAULTS
from litmus.spec.card import level_word

DAY = date(2026, 9, 11)
MUYUAN = "002714.SZ"  # 牧原股份，农林牧渔 / 养殖业，2026 上半年亏损
ST_STOCK = "000911.SZ"  # 农林牧渔里当天的 ST 股
SUSPENDED = "603159.SH"  # 当天停牌

_ds = DataService.from_env()
try:
    _first, _last = _ds.data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的股票日频数据", allow_module_level=True)
if _last < DAY:
    pytest.skip(f"用例要 {DAY} 的数据，本地只到 {_last}", allow_module_level=True)

_events = load_events()


def card(metrics: list[dict], codes: list[str] | None = None, **scope):
    raw = {
        "scope": {"target": "stock", **scope},
        "subject": {"kind": "codes", "codes": codes or [MUYUAN]},
        "when": {"as_of": DAY.isoformat()},
        "metrics": metrics,
        "output": {"kind": "card"},
    }
    spec, issues = check_spec(raw, _ds, _events)
    assert spec is not None, [issue_text(issue) for issue in issues]
    return run_card(spec, _ds)


def rows(result, index: int = 0) -> dict[str, tuple[str, str]]:
    return {row.name: (row.text, row.note) for row in result.items[index].rows}


def value(expr: str, code: str = MUYUAN, target: str = "stock"):
    """同一个日子、同一只标的，单独再算一遍这个公式。"""
    pool = pl.DataFrame(
        {"date": [DAY], "code": [code]}, schema={"date": pl.Date, "code": pl.String}
    )
    values = evaluate(expr, "metric", pool, DAY, DAY, _ds, target=target).values
    return None if values.is_empty() else values.get_column("value").item()


# ── A101 牧原股份现在市盈率多少，两年里算高算低 ────────────────


A101 = [
    {"name": "市盈率TTM", "expr": "$pe_ttm"},
    {"name": "市盈率两年分位", "expr": "TsRank($pe_ttm, 500)"},
    {"name": "市净率", "expr": "$pb"},
    {"name": "市净率两年分位", "expr": "TsRank($pb, 500)"},
]


def test_A101_分位并进那个指标的解释行_不单独占一行():
    result = card(A101)
    assert [row.name for row in result.items[0].rows] == ["市盈率TTM", "市净率"]


def test_A101_市净率的解释行是分位和两年区间():
    text, note = rows(card(A101))["市净率"]
    percentile = round(value("TsRank($pb, 500)") * 100)
    low, high = value("Min($pb, 500)"), value("Max($pb, 500)")
    assert text == f"{value('$pb'):.2f}"
    level = level_word(value("TsRank($pb, 500)"))
    assert note == f"两年分位 {percentile}%，{level}；两年区间 {low:.2f} ~ {high:.2f}"


def test_A101_亏损股没有市盈率_说清是哪一期亏的():
    text, note = rows(card(A101))["市盈率TTM"]
    report = _ds.latest_reports([MUYUAN], DAY).row(0, named=True)
    assert value("$pe_ttm") is None and text == "无"
    assert note.startswith(f"{report['period'].year} 上半年亏损（净利同比 ")
    assert note.endswith("亏损股没有市盈率，两年分位也算不出")


def test_A101_卡上的标题是名称代码和行业():
    item = card(A101).items[0]
    assert (item.code, item.name, item.industry) == (MUYUAN, "牧原股份", "农林牧渔 / 养殖业")


def test_分位只按有值的那些天算_写出亏损的天数不算():
    """2026-03-02 牧原有市盈率，但两年窗口里有亏损没有市盈率的日子。"""
    day = date(2026, 3, 2)
    raw = {
        "subject": {"kind": "codes", "codes": [MUYUAN]},
        "when": {"as_of": day.isoformat()},
        "metrics": A101[:2],
        "output": {"kind": "card"},
    }
    spec, issues = check_spec(raw, _ds, _events)
    assert spec is not None, [issue_text(issue) for issue in issues]
    text, note = rows(run_card(spec, _ds))["市盈率TTM"]

    window = _ds.get_fields([MUYUAN], day, day, ["pe_ttm"], lookback=499).get_column("pe_ttm")
    have, now = window.drop_nulls(), window[-1]
    assert window.null_count() > 0 and now is not None  # 用例成立的前提
    place = (have < now).sum() + ((have == now).sum() + 1) / 2
    percentile = round(place / have.len() * 100)
    assert text == f"{now:.2f}"
    assert note == (
        f"两年分位 {percentile}%，{level_word(place / have.len())}"
        f"（两年里 {have.len()} 天有市盈率TTM，亏损的 {window.null_count()} 天不算）；"
        f"两年区间 {have.min():.2f} ~ {have.max():.2f}"
    )


def test_空值都说得出为什么():
    result = card([*A101, {"name": "今年以来涨幅", "expr": "PctSince($close, 20251231)"}])
    empty = [row for row in result.items[0].rows if row.text == "无"]
    assert empty and all(row.note for row in empty)


# ── A201 牧原在农林牧渔里涨幅排第几 ────────────────────────────


A201 = [{"name": "今年以来涨幅排名", "expr": "Rank(PctSince($close, 20251231))"}]


def test_A201_排名在算的范围里数出来():
    result = card(A201, industry="农林牧渔")
    pool = _ds.get_universe_mask(DAY, DAY, industry="农林牧渔", exclude=list(DEFAULTS["exclude"]))
    changes = (
        evaluate("PctSince($close, 20251231)", "metric", pool, DAY, DAY, _ds)
        .values.drop_nulls("value")
        .sort("value", descending=True)
    )
    codes = changes.get_column("code").to_list()
    text, _ = rows(result)["今年以来涨幅排名"]
    assert result.pool_size == pool.height
    assert text == f"第 {codes.index(MUYUAN) + 1} 名（共 {len(codes)} 只）"


def test_A201_解释行写涨了多少_并和同期行业比():
    _, note = rows(card(A201, industry="农林牧渔"))["今年以来涨幅排名"]
    mine = value("PctSince($close, 20251231)")
    industry = value("PctSince($close, 20251231)", code="801010.SI", target="sw_industry")
    assert note == f"今年以来 {mine * 100:.2f}%，同期农林牧渔行业指数 {industry * 100:.2f}%"


def test_跑赢沪深300了吗_涨跌和指数比():
    raw = {
        "subject": {"kind": "codes", "codes": [MUYUAN]},
        "when": {"as_of": DAY.isoformat()},
        "metrics": [{"name": "今年以来涨幅", "expr": "PctSince($close, 20251231)"}],
        "output": {"kind": "card", "benchmark": "index:000300.SH"},
    }
    spec, issues = check_spec(raw, _ds, _events)
    assert spec is not None, [issue_text(issue) for issue in issues]
    _, note = rows(run_card(spec, _ds))["今年以来涨幅"]
    index = _ds.get_index_daily("000300.SH", date(2025, 12, 1), DAY)
    base = index.filter(pl.col("date") <= date(2025, 12, 31)).get_column("close")[-1]
    now = index.filter(pl.col("date") == DAY).get_column("close").item()
    assert note == f"同期沪深300 指数 {(now / base - 1) * 100:+.2f}%"


def test_当日涨跌幅也和同期对照比():
    """2026-09-18 实测：「跑赢沪深 300 了吗」写成 $pct_chg，卡上只剩一个数，没有对照。"""
    raw = {
        "subject": {"kind": "codes", "codes": [MUYUAN]},
        "when": {"as_of": DAY.isoformat()},
        "metrics": [{"name": "当日涨跌", "expr": "$pct_chg"}],
        "output": {"kind": "card", "benchmark": "index:000300.SH"},
    }
    spec, _ = check_spec(raw, _ds, _events)
    text, note = rows(run_card(spec, _ds))["当日涨跌"]
    closes = _ds.get_index_daily("000300.SH", date(2026, 9, 1), DAY).get_column("close")
    assert text == f"{value('$pct_chg'):+.2f}%"
    assert note == f"同期沪深300 指数 {(closes[-1] / closes[-2] - 1) * 100:+.2f}%"

    industry = card([{"name": "当日涨跌", "expr": "$pct_chg"}])
    daily = value("$pct_chg", code="801010.SI", target="sw_industry")
    assert rows(industry)["当日涨跌"][1] == f"同期农林牧渔行业指数 {daily:+.2f}%"


def test_ST股照样出卡_写明被算的范围剔除了():
    result = card(A201, codes=[ST_STOCK], industry="农林牧渔")
    text, note = rows(result)["今年以来涨幅排名"]
    assert (text, note) == ("无", "当天是 ST 股，算的范围把它剔除了，不参与排名")


def test_查不到的代码_限定了行业也照样报出来():
    raw = {
        "scope": {"target": "stock", "industry": "农林牧渔"},
        "subject": {"kind": "codes", "codes": ["999999.SZ"]},
        "when": {"as_of": DAY.isoformat()},
        "metrics": A201,
        "output": {"kind": "card"},
    }
    spec, issues = check_spec(raw, _ds, _events)
    assert spec is None
    assert [issue_text(issue) for issue in issues] == [
        "subject.codes：999999.SZ：本地没有这个股票代码，代码要带交易所后缀，如 600519.SH"
    ]


def test_不在这个行业里的股票_报出来():
    spec, issues = check_spec(
        {
            "scope": {"target": "stock", "industry": "银行"},
            "subject": {"kind": "codes", "codes": [MUYUAN]},
            "when": {"as_of": DAY.isoformat()},
            "metrics": A201,
            "output": {"kind": "card"},
        },
        _ds,
        _events,
    )
    assert spec is None and "不在算的范围里" in issue_text(issues[0])


def test_停牌的照样出卡_写明停牌前最后一个交易日():
    text, note = rows(card([{"name": "市净率", "expr": "$pb"}], codes=[SUSPENDED]))["市净率"]
    last = _ds.get_fields([SUSPENDED], DAY, DAY, ["close"], lookback=1).get_column("date").max()
    assert (text, note) == ("无", f"当天停牌，没有行情，停牌前最后一个交易日是 {last}")
