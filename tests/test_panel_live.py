"""用真数据验证面板拼装：找一个真实的除权日，确认后复权价连续、不复权价断崖。

没配 TUSHARE_TOKEN 时自动跳过。
"""

from __future__ import annotations

import os

import polars as pl
import pytest

from litmus.data.loaders.normalize import (
    build_daily_panel,
    normalize_adj_factor,
    normalize_daily,
    normalize_daily_basic,
    normalize_stk_limit,
    normalize_stock_st,
)
from litmus.data.loaders.tushare import TushareClient, TushareConfig

pytestmark = pytest.mark.skipif(
    not os.getenv("TUSHARE_TOKEN"), reason="需要 .env 里的 TUSHARE_TOKEN"
)

CODE = "600519.SH"  # 贵州茅台，期间有多次分红除权
START, END = "20230101", "20241231"


@pytest.fixture(scope="module")
def panel() -> pl.DataFrame:
    with TushareClient(TushareConfig.from_env()) as client:
        window = {"ts_code": CODE, "start_date": START, "end_date": END}
        return build_daily_panel(
            normalize_daily(client.call("daily", window)),
            normalize_adj_factor(client.call("adj_factor", window)),
            normalize_daily_basic(client.call("daily_basic", window)),
            normalize_stk_limit(client.call("stk_limit", window)),
            normalize_stock_st(client.call("stock_st", window)),
        )


def test_两年数据行数合理(panel):
    assert 400 < panel.height < 520, f"两年应该有 480 个左右交易日，实际 {panel.height}"


def test_除权日后复权连续而不复权断崖(panel):
    """复权因子变化的那天就是除权日。"""
    df = panel.with_columns(
        (pl.col("adj_factor") / pl.col("adj_factor").shift(1)).alias("factor_ratio"),
        (pl.col("close") / pl.col("close").shift(1) - 1).alias("hfq_return"),
        (pl.col("close_raw") / pl.col("close_raw").shift(1) - 1).alias("raw_return"),
    ).drop_nulls("factor_ratio")

    ex_div_days = df.filter(pl.col("factor_ratio") > 1.0001)
    assert ex_div_days.height > 0, "两年里应该至少有一次除权除息"

    for row in ex_div_days.iter_rows(named=True):
        # 后复权后的涨跌幅必须落在涨跌停范围内；不复权价会凭空跌掉一笔分红
        assert abs(row["hfq_return"]) < 0.11, f"{row['date']} 后复权涨跌幅异常：{row['hfq_return']}"
        assert row["raw_return"] < row["hfq_return"], f"{row['date']} 不复权价没有出现除权缺口"


def test_后复权涨跌幅与接口给的涨跌幅一致(panel):
    """daily.pct_chg 本身就是按除权后昨收算的，应该和我们算的后复权涨跌幅对得上。"""
    df = panel.with_columns(
        ((pl.col("close") / pl.col("close").shift(1) - 1) * 100).alias("computed_pct")
    ).drop_nulls("computed_pct")

    diff = df.select((pl.col("computed_pct") - pl.col("pct_chg")).abs().max()).item()
    assert diff < 0.02, f"与接口的涨跌幅最大差 {diff:.4f} 个百分点"


def test_涨停标志与涨跌幅自洽(panel):
    limit_ups = panel.filter(pl.col("is_limit_up"))
    for row in limit_ups.iter_rows(named=True):
        assert row["pct_chg"] > 9.0, f"{row['date']} 标成涨停但当天只涨了 {row['pct_chg']}%"
