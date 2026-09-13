"""单位归一与面板拼装的小表格测试：手写几行数、手算正确答案，不联网。"""

from __future__ import annotations

import pytest

from litmus.data.loaders.normalize import (
    NormalizeError,
    build_daily_panel,
    normalize_adj_factor,
    normalize_daily,
    normalize_daily_basic,
    normalize_stk_limit,
    normalize_stock_st,
)


def daily_rows(**overrides):
    row = {
        "ts_code": "600519.SH",
        "trade_date": "20260911",
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "pct_chg": 5.0,
        "vol": 1000.0,  # 手
        "amount": 1050.0,  # 千元
    }
    return [{**row, **overrides}]


def adj_rows(factor=1.0, date="20260911"):
    return [{"ts_code": "600519.SH", "trade_date": date, "adj_factor": factor}]


def basic_rows(date="20260911"):
    return [
        {
            "ts_code": "600519.SH",
            "trade_date": date,
            "turnover_rate": 2.5,
            "pe_ttm": 30.0,
            "pb": 8.0,
            "ps_ttm": 12.0,
            "dv_ttm": 1.5,
            "total_mv": 12345.0,  # 万元
            "circ_mv": 10000.0,  # 万元
        }
    ]


def limit_rows(up=11.55, down=9.45, date="20260911"):
    return [{"ts_code": "600519.SH", "trade_date": date, "up_limit": up, "down_limit": down}]


def st_rows(*codes, date="20260911"):
    return [{"ts_code": code, "trade_date": date} for code in codes]


def panel_of(daily, adj, basic, limit, st=()):
    return build_daily_panel(
        normalize_daily(daily),
        normalize_adj_factor(adj),
        normalize_daily_basic(basic),
        normalize_stk_limit(limit),
        normalize_stock_st(st),
    )


def test_成交量从手换成股():
    row = panel_of(daily_rows(), adj_rows(), basic_rows(), limit_rows()).row(0, named=True)
    assert row["volume"] == 1000.0 * 100


def test_成交额从千元换成元():
    row = panel_of(daily_rows(), adj_rows(), basic_rows(), limit_rows()).row(0, named=True)
    assert row["amount"] == 1050.0 * 1000


def test_市值从万元换成元():
    row = panel_of(daily_rows(), adj_rows(), basic_rows(), limit_rows()).row(0, named=True)
    assert row["market_cap"] == 12345.0 * 10_000
    assert row["circ_mv"] == 10000.0 * 10_000


def test_后复权价等于原始价乘复权因子():
    row = panel_of(daily_rows(), adj_rows(factor=2.0), basic_rows(), limit_rows()).row(
        0, named=True
    )
    assert row["close"] == pytest.approx(21.0)
    assert row["close_raw"] == pytest.approx(10.5)
    assert row["open"] == pytest.approx(20.0)


def test_成交均价与收盘价同为后复权口径():
    # 成交额 1050 千元 = 105 万元，成交股数 10 万股 → 原始均价 10.5 元
    row = panel_of(daily_rows(), adj_rows(factor=2.0), basic_rows(), limit_rows()).row(
        0, named=True
    )
    assert row["vwap"] == pytest.approx(21.0)


def test_送转日后复权价连续而不复权价断崖():
    """10 送 10：股价减半、股数翻倍，后复权价和复权成交量都应该保持连续。"""
    before = panel_of(
        daily_rows(trade_date="20260910", close=20.0, vol=1000.0, amount=2000.0),
        adj_rows(factor=1.0, date="20260910"),
        basic_rows(date="20260910"),
        limit_rows(date="20260910"),
    ).row(0, named=True)
    after = panel_of(
        daily_rows(trade_date="20260911", close=10.2, vol=2000.0, amount=2040.0),
        adj_rows(factor=2.0, date="20260911"),
        basic_rows(date="20260911"),
        limit_rows(date="20260911"),
    ).row(0, named=True)

    # 不复权价看起来腰斩
    assert after["close_raw"] / before["close_raw"] == pytest.approx(0.51)
    # 后复权价只涨了 2%
    assert after["close"] / before["close"] == pytest.approx(1.02)
    # 复权成交量保持在同一量级，不会被误判成"放量"
    assert after["volume"] == pytest.approx(before["volume"])


def test_收盘价等于涨停价即为涨停():
    panel = panel_of(daily_rows(close=11.55), adj_rows(), basic_rows(), limit_rows(up=11.55))
    row = panel.row(0, named=True)
    assert row["is_limit_up"] is True
    assert row["is_limit_down"] is False


def test_浮点误差不影响涨停判断():
    panel = panel_of(
        daily_rows(close=11.550000000000001), adj_rows(), basic_rows(), limit_rows(up=11.55)
    )
    assert panel.row(0, named=True)["is_limit_up"] is True


def test_开盘即涨停单独标记():
    panel = panel_of(
        daily_rows(open=11.55, close=10.5), adj_rows(), basic_rows(), limit_rows(up=11.55)
    )
    row = panel.row(0, named=True)
    assert row["open_limit_up"] is True
    assert row["is_limit_up"] is False


def test_缺复权因子直接报错而不是算出空价格():
    with pytest.raises(NormalizeError, match="复权因子"):
        panel_of(daily_rows(), [], basic_rows(), limit_rows())


def test_原始返回缺字段直接报错():
    broken = daily_rows()
    del broken[0]["amount"]
    with pytest.raises(NormalizeError, match="amount"):
        normalize_daily(broken)


def test_在ST名单里的股票标记为ST():
    panel = panel_of(daily_rows(), adj_rows(), basic_rows(), limit_rows(), st_rows("600519.SH"))
    assert panel.row(0, named=True)["is_st"] is True


def test_不在名单里是明确的不是ST而不是空值():
    """空值会让「排除 ST」这类筛选条件静默失效，必须是 False。"""
    panel = panel_of(daily_rows(), adj_rows(), basic_rows(), limit_rows(), st_rows("000001.SZ"))
    assert panel.row(0, named=True)["is_st"] is False


def test_ST名单为空时全市场都不是ST():
    panel = panel_of(daily_rows(), adj_rows(), basic_rows(), limit_rows())
    assert panel.row(0, named=True)["is_st"] is False


def test_ST名单可以覆盖比面板更长的区间():
    """按月拉的名单包含面板里没有的日期，join 要按 (code, date) 对上，不能串行。"""
    st = st_rows("600519.SH", date="20260910") + st_rows("600519.SH", date="20260911")
    panel = panel_of(daily_rows(), adj_rows(), basic_rows(), limit_rows(), st)
    assert panel.height == 1
    assert panel.row(0, named=True)["is_st"] is True


def test_别的日子是ST不影响今天():
    st = st_rows("600519.SH", date="20260910")
    panel = panel_of(daily_rows(), adj_rows(), basic_rows(), limit_rows(), st)
    assert panel.row(0, named=True)["is_st"] is False


def test_ST名单重复不会让面板多出行():
    st = st_rows("600519.SH") + st_rows("600519.SH")
    panel = panel_of(daily_rows(), adj_rows(), basic_rows(), limit_rows(), st)
    assert panel.height == 1


def test_停牌日不补行():
    """daily 里没有的日子就是停牌，面板里也不该凭空出现。"""
    panel = panel_of(
        daily_rows(trade_date="20260911"),
        adj_rows(date="20260911") + adj_rows(date="20260910"),  # 复权因子两天都有
        basic_rows(date="20260911"),
        limit_rows(date="20260911"),
    )
    assert panel.height == 1
    assert str(panel.row(0, named=True)["date"]) == "2026-09-11"


def test_北交所照常落盘_不在这一层过滤():
    panel = panel_of(
        daily_rows(ts_code="920992.BJ"),
        [{"ts_code": "920992.BJ", "trade_date": "20260911", "adj_factor": 1.0}],
        [{**basic_rows()[0], "ts_code": "920992.BJ"}],
        [{**limit_rows()[0], "ts_code": "920992.BJ"}],
    )
    assert panel.row(0, named=True)["code"] == "920992.BJ"
