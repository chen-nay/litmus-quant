"""卡上的文字（DESIGN.md §1.5）。不联网、不读数据——数和原因都手写。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from litmus.spec.card import (
    CHANGE,
    CONDITION,
    DELISTED,
    EXCLUDED,
    GAPS,
    LOSS,
    NO_REPORT,
    NO_START,
    NO_VALUE,
    NOT_LISTED,
    PERCENTILE,
    RANK,
    RATIO,
    SHORT,
    SUSPENDED,
    VALUE,
    MetricFacts,
    Missing,
    Report,
    format_value,
    level_word,
    missing_text,
    period_word,
    render_row,
    window_word,
)

PB = MetricFacts(kind=VALUE, value=2.9955, unit="倍", field="pb", label="市净率")


PERCENTILE_FACTS = MetricFacts(
    kind=PERCENTILE,
    value=0.284,
    unit=RATIO,
    label="市净率在近 500 日里的分位",
    window=500,
    low=2.1657,
    high=4.1465,
    inner=PB,
)


def percentile(**changes) -> MetricFacts:
    return replace(PERCENTILE_FACTS, **changes)


# ── 值怎么写 ────────────────────────────────────────────────────


def test_金额按亿万_涨跌带正负号_空值写无():
    assert format_value(8.4e8, "元", "amount") == "8.40亿"
    assert format_value(-0.178913, RATIO) == "-17.89%"
    assert format_value(0.0312, RATIO) == "+3.12%"
    assert format_value(-157.7172, "%", "profit_yoy") == "-157.72%"
    assert format_value(2.9955, "倍", "pb") == "3.00"
    assert format_value(None) == "无"
    assert (format_value(True), format_value(False)) == ("是", "否")


def test_排名写成第几名_并列写并列():
    facts = MetricFacts(kind=RANK, value=0.26, unit=RATIO, position=75, total=100, measure="只")
    assert render_row(facts)[0] == "第 75 名（共 100 只）"
    assert render_row(replace(facts, tied=True))[0] == "并列第 75 名（共 100 只）"


def test_分位写成整百分比():
    assert render_row(percentile())[0] == "28%"


# ── 解释行 ──────────────────────────────────────────────────────


def test_分位并进那个数的解释行_说高低再给区间():
    row = replace(PB, percentile=percentile())
    assert render_row(row) == ("3.00", "两年分位 28%，偏低；两年区间 2.17 ~ 4.15")


def test_那个数是空的_分位也算不出():
    empty = replace(PB, value=None, missing=Missing(NO_VALUE, label="市净率"))
    row = replace(empty, percentile=replace(percentile(), value=None))
    assert render_row(row) == ("无", "当天没有市净率，两年分位也算不出")


def test_亏损股没有市盈率_写出是哪一期亏的():
    report = Report(period=date(2026, 6, 30), roe=-15.3986, profit_yoy=-157.7172)
    facts = MetricFacts(
        kind=VALUE,
        value=None,
        unit="倍",
        field="pe_ttm",
        label="市盈率TTM",
        missing=Missing(LOSS, label="市盈率TTM", report=report),
        percentile=replace(percentile(), value=None),
    )
    assert render_row(facts) == (
        "无",
        "2026 上半年亏损（净利同比 -157.72%），亏损股没有市盈率，两年分位也算不出",
    )


def test_最新一期没亏损时只说过去四个季度():
    report = Report(period=date(2026, 6, 30), roe=3.2, profit_yoy=-20.0)
    assert missing_text(Missing(LOSS, report=report)) == "过去四个季度合计亏损，亏损股没有市盈率"
    assert missing_text(Missing(LOSS)) == "过去四个季度合计亏损，亏损股没有市盈率"


def test_分位单独占一行时_区间后面写现在多少():
    text, note = render_row(percentile())
    assert (text, note) == ("28%", "偏低；市净率两年区间 2.17 ~ 4.15，现在 3.00")


def test_窗口里有几天没有值_写出算了几天():
    pe = MetricFacts(kind=VALUE, value=12.1076, unit="倍", field="pe_ttm", label="市盈率TTM")
    facts = percentile(value=0.47, low=8.48, high=31.11, counted=379, inner=pe)
    assert render_row(replace(pe, percentile=facts))[1] == (
        "两年分位 47%，处在中间（两年里 379 天有市盈率TTM，亏损的 121 天不算）；两年区间 8.48 ~ 31.11"
    )
    assert render_row(facts)[1] == (
        "处在中间（两年里 379 天有市盈率TTM，亏损的 121 天不算）；"
        "市盈率TTM两年区间 8.48 ~ 31.11，现在 12.11"
    )
    # 不是市盈率就不说「亏损」
    assert "没有市净率的 100 天不算" in render_row(percentile(counted=400))[1]
    # 每天都有值就不提
    assert "不算" not in render_row(percentile(counted=500))[1]


def test_分位算不出时说为什么():
    missing = Missing(GAPS, label="市盈率TTM", need=500, have=121)
    note = render_row(replace(percentile(), value=None, missing=missing))[1]
    assert note == "最近 500 个交易日里有 121 天没有市盈率TTM，算不出分位"


def test_涨跌给同期行业做对照():
    facts = MetricFacts(
        kind=CHANGE,
        value=-0.178913,
        unit=RATIO,
        period="今年以来",
        benchmark=("农林牧渔行业指数", -0.145183),
    )
    assert render_row(facts) == ("-17.89%", "同期农林牧渔行业指数 -14.52%")


def test_排名的解释行写被排的那个数():
    inner = MetricFacts(
        kind=CHANGE,
        value=-0.178913,
        unit=RATIO,
        period="今年以来",
        benchmark=("农林牧渔行业指数", -0.145183),
    )
    facts = MetricFacts(kind=RANK, value=0.26, position=75, total=100, inner=inner)
    assert render_row(facts)[1] == "今年以来 -17.89%，同期农林牧渔行业指数 -14.52%"


def test_被算的范围剔除时不参与排名():
    facts = MetricFacts(
        kind=RANK, value=None, missing=Missing(EXCLUDED, excluded="是 ST 股"), total=100
    )
    assert render_row(facts) == ("无", "当天是 ST 股，算的范围把它剔除了，不参与排名")


def test_条件写比较的两边_都是后复权价时说高低百分之几():
    left = MetricFacts(kind=VALUE, value=824.63, unit="元", field="close", label="收盘价")
    right = MetricFacts(kind=VALUE, value=893.19, unit="元", label="近 250 日收盘价均值")
    facts = MetricFacts(kind=CONDITION, value=False, sides=(left, right), relative=True)
    assert render_row(facts) == ("否", "收盘价比近 250 日收盘价均值低 7.68%")


def test_条件不是价格时各写各的值():
    left = MetricFacts(kind=VALUE, value=12.1076, unit="倍", field="pe_ttm", label="市盈率TTM")
    facts = MetricFacts(kind=CONDITION, value=True, sides=(left,))
    assert render_row(facts) == ("是", "市盈率TTM 12.11")


def test_后复权价写出当天的实际成交价():
    facts = MetricFacts(
        kind=VALUE,
        value=824.63,
        unit="元",
        field="close",
        label="收盘价",
        adjusted=True,
        raw_close=41.08,
    )
    assert render_row(facts)[1] == "后复权价，当天实际收盘价 41.08 元"


def test_财务数写出是哪一期财报():
    report = Report(date(2026, 6, 30), -15.4, -157.7172, announced=date(2026, 8, 21))
    facts = MetricFacts(kind=VALUE, value=-157.7172, unit="%", field="profit_yoy", report=report)
    assert render_row(facts) == ("-157.72%", "2026 上半年财报，2026-08-21 公告")


# ── 空值的原因 ──────────────────────────────────────────────────


def test_每种原因都说得出一句话():
    assert (
        missing_text(Missing(NOT_LISTED, day=date(2026, 9, 1))) == "当天还没上市，2026-09-01 才上市"
    )
    assert missing_text(Missing(DELISTED, day=date(2020, 5, 8))) == "2020-05-08 已经退市"
    assert (
        missing_text(Missing(SUSPENDED, day=date(2026, 9, 10)))
        == "当天停牌，没有行情，停牌前最后一个交易日是 2026-09-10"
    )
    assert missing_text(Missing(NO_REPORT)) == "还没有公告过财报"
    assert (
        missing_text(Missing(NO_START, day=date(2025, 12, 31))) == "2025-12-31 时还没上市，没有起点"
    )
    assert (
        missing_text(Missing(SHORT, need=500, have=120)) == "要最近 500 个交易日的行情，只有 120 个"
    )


# ── 词 ──────────────────────────────────────────────────────────


def test_窗口天数说成年月():
    assert [window_word(n) for n in (5, 20, 60, 125, 250, 500)] == [
        "一周",
        "一个月",
        "三个月",
        "半年",
        "一年",
        "两年",
    ]
    assert window_word(130) == "近 130 个交易日"


def test_报告期说成上半年():
    assert period_word(date(2026, 6, 30)) == "2026 上半年"
    assert period_word(date(2026, 3, 31)) == "2026 一季度"
    assert period_word(date(2025, 12, 31)) == "2025 全年"


def test_分位说成高低():
    assert [level_word(p) for p in (0.05, 0.28, 0.5, 0.75, 0.95)] == [
        "处在低位",
        "偏低",
        "处在中间",
        "偏高",
        "处在高位",
    ]
