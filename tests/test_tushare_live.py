"""需要真 token 的联调测试。没配 TUSHARE_TOKEN 时自动跳过。

跑法：uv run pytest tests/test_tushare_live.py -v
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from litmus.data.loaders.tushare import TushareClient, TushareConfig

pytestmark = pytest.mark.skipif(
    not os.getenv("TUSHARE_TOKEN"), reason="需要 .env 里的 TUSHARE_TOKEN"
)


@pytest.fixture(scope="module")
def client():
    with TushareClient(TushareConfig.from_env()) as c:
        yield c


@pytest.fixture(scope="module")
def latest_trading_day(client) -> str:
    """最近一个交易日（只看日历，不代表本地已有数据）。"""
    today = dt.date.today()
    rows = client.call(
        "trade_cal",
        {
            "exchange": "SSE",
            "start_date": (today - dt.timedelta(days=20)).strftime("%Y%m%d"),
            "end_date": today.strftime("%Y%m%d"),
        },
        "cal_date,is_open",
    )
    open_days = [r["cal_date"] for r in rows if str(r["is_open"]) == "1"]
    assert open_days, "拿不到交易日历"
    return max(open_days)


def test_trade_cal_字段与预期一致(client, latest_trading_day):
    assert len(latest_trading_day) == 8 and latest_trading_day.isdigit()


def test_daily_basic_分页能把一天取全(client, latest_trading_day):
    """单日全市场约 5500 行。用小 page_size 强制翻页，验证分页确实有效。"""
    rows = client.call(
        "daily_basic",
        {"trade_date": latest_trading_day},
        "ts_code,turnover_rate,total_mv",
        page_size=2000,
    )

    assert len(rows) > 4000, f"只取到 {len(rows)} 行，分页可能没生效"
    codes = [r["ts_code"] for r in rows]
    assert len(codes) == len(set(codes)), "出现重复行，说明 offset 分页有问题"
    assert set(rows[0]) == {"ts_code", "turnover_rate", "total_mv"}


def test_返回数据里确实混着北交所(client, latest_trading_day):
    """设计上不含北交所，loader 必须显式过滤——这里确认它确实会混进来。"""
    rows = client.call("daily_basic", {"trade_date": latest_trading_day}, "ts_code", page_size=6000)
    assert any(r["ts_code"].endswith(".BJ") for r in rows)


def test_probe_对可用接口返回True(client):
    available, reason = client.probe("tdx_index", {"trade_date": "20260911"})
    assert available is True, reason
