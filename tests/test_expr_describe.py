"""表达式 → 中文说明（expr.describe）：逐个算子核对说法和括号。"""

from __future__ import annotations

import pytest

from litmus.expr import describe
from litmus.expr.operators import OPERATORS


@pytest.mark.parametrize(
    ("expr", "text"),
    [
        ("$close > 10", "收盘价 > 10"),
        ("$pct_chg > 9 & $pe_ttm < 30", "当日涨跌幅 > 9 且 市盈率TTM < 30"),
        ("($pct_chg > 9) | ~$is_st", "当日涨跌幅 > 9 或 不满足ST / *ST"),
        ("~($pct_chg > 9)", "不满足（当日涨跌幅 > 9）"),
        (
            "$amount > Mean(Ref($amount, 1), 5) * 1.4",
            "成交额 > 前 5 日成交额均值（不含当天） × 1.4",
        ),
        ("$amount / Mean(Ref($amount, 1), 20)", "成交额 ÷ 前 20 日成交额均值（不含当天）"),
        ("Cross($close, Mean($close, 250))", "收盘价上穿近 250 日收盘价均值"),
        ("($close - $open) / $open", "（收盘价 - 开盘价） ÷ 开盘价"),
        ("$close - ($open - $low)", "收盘价 - （开盘价 - 最低价）"),
        ("$close - $open - $low", "收盘价 - 开盘价 - 最低价"),
        ("Mean($close - $open, 5)", "近 5 日（收盘价 - 开盘价）均值"),
        ("Ref($close, 1)", "前一交易日的收盘价"),
        ("Ref($close, 5)", "5 个交易日前的收盘价"),
        ("Mean(Ref($amount, 5), 20)", "5 个交易日前往前数 20 日的成交额均值"),
        ("EMA($close, 12) - EMA($close, 26)", "收盘价的 12 日指数均线 - 收盘价的 26 日指数均线"),
        ("Count($is_limit_up, 3) == 3", "近 3 日里收盘涨停的天数 = 3"),
        ("Std($pct_chg, 20)", "近 20 日当日涨跌幅标准差"),
        ("Sum($amount, 5)", "近 5 日成交额合计"),
        ("$close >= Max($close, 250)", "收盘价 ≥ 近 250 日收盘价最高"),
        ("Min($close, 60)", "近 60 日收盘价最低"),
        ("Delta($close, 5)", "收盘价较 5 个交易日前的变化"),
        ("Pct($close, 20)", "收盘价较 20 个交易日前的涨跌幅"),
        ("TsRank($amount, 60)", "成交额在近 60 日里的分位"),
        ("Rank($amount)", "成交额在当天股票池里的分位"),
        ("If($pct_chg > 0, $amount, 0)", "如果（当日涨跌幅 > 0），取成交额，否则取 0"),
        ("Abs($pct_chg)", "当日涨跌幅的绝对值"),
        ("Log($amount)", "成交额的自然对数"),
        ("Sign($pct_chg)", "当日涨跌幅的正负（1、0、-1）"),
        ("-$pct_chg", "-当日涨跌幅"),
        ("$close != 1.40", "收盘价 ≠ 1.40"),
        ("$market_cap < 30000000000", "总市值 < 300 亿"),
        ("$market_cap < 30亿 & $amount > 5000万", "总市值 < 30 亿 且 成交额 > 5000 万"),
        ("$amount > 123456789", "成交额 > 1.2346 亿"),
        ("Pct($close, 20) > 0.1", "收盘价较 20 个交易日前的涨跌幅 > 10%"),
        ("Pct($close, 5) < -0.05", "收盘价较 5 个交易日前的涨跌幅 < -5%"),
        ("0.155 <= Pct($close, 60)", "15.5% ≤ 收盘价较 60 个交易日前的涨跌幅"),
        ("PctSince($close, 20251231) > 0.5", "收盘价从 2025-12-31 到当天的涨跌幅 > 50%"),
    ],
)
def test_说法(expr, text):
    assert describe(expr) == text


def test_板块表的排名是在全部板块里排():
    assert describe("Rank($pct_chg)", target="sw_industry") == "当日涨跌幅在当天全部板块里的分位"


def test_全部算子都有说法_不会原样吐出算子名():
    samples = {
        "Mean": "Mean($close, 5)",
        "EMA": "EMA($close, 5)",
        "Std": "Std($close, 5)",
        "Sum": "Sum($close, 5)",
        "Max": "Max($close, 5)",
        "Min": "Min($close, 5)",
        "Ref": "Ref($close, 5)",
        "Delta": "Delta($close, 5)",
        "Pct": "Pct($close, 5)",
        "PctSince": "PctSince($close, 20251231)",
        "TsRank": "TsRank($close, 5)",
        "Count": "Count($is_st, 5)",
        "Cross": "Cross($close, $open)",
        "Rank": "Rank($close)",
        "If": "If($is_st, 1, 0)",
        "Abs": "Abs($close)",
        "Log": "Log($close)",
        "Sign": "Sign($close)",
    }
    assert set(samples) == set(OPERATORS)
    for name, expr in samples.items():
        assert f"{name}（" not in describe(expr), expr


# ── 算出来的数怎么显示 ──────────────────────────────────────────


def test_偏离和回撤按百分比显示_天数按整数():
    """2026-09-18 实测「比最高点跌了多少」写成 1 - $close / Max($close, 1000)，卡上显示成 0.28。"""
    from litmus.expr import RATIO, result_unit

    assert result_unit("1 - $close / Max($close, 1000)") == RATIO
    assert result_unit("$close / Mean($close, 250) - 1") == RATIO
    assert result_unit("Count($is_limit_up, 10)") == "个"
    assert result_unit("$close / Mean($close, 250)") == ""  # 倍数不是涨跌
    assert result_unit("$market_cap") == "元"
    from litmus.expr import PERCENTILE

    assert result_unit("TsRank($pe_ttm, 500)") == result_unit("Rank($amount)") == PERCENTILE
    assert result_unit("PctSince($close, 20251231)") == RATIO
