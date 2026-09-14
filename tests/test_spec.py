"""QuerySpec 的测试：文档示例能解析，结构和数值范围不对的拦下。不读数据。"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from litmus.spec import (
    DEFAULTS,
    BoardListSpec,
    StockHistorySpec,
    StockListSpec,
    parse_spec,
)

# ARCHITECTURE §4.1 的三个示例，原样抄来
STOCK_LIST = {
    "version": 1,
    "shape": "stock_list",
    "as_of": "2026-09-11",
    "filter": {
        "expr": "$amount > Mean(Ref($amount,1), 5) * 1.4",
        "label": "成交额比前一周均值高 40%",
    },
    "universe": {
        "base": "all_a",
        "industry": None,
        "board": None,
        "exclude": ["ST", "suspended", "new_listing_60d"],
    },
    "sort": {"by": "$amount / Mean(Ref($amount,1), 5)", "order": "desc"},
    "limit": 50,
    "defaults_used": ["limit"],
    "assumptions": ["「成交量」理解为成交额（元）", "「前一周」按 5 个交易日计算，不含当天"],
}
BOARD_LIST = {
    "version": 1,
    "shape": "board_list",
    "board_type": "concept",
    "as_of": "2026-09-11",
    "filter": None,
    "sort": {"by": "Sum($amount, 5)", "order": "desc"},
    "limit": 10,
    "assumptions": ["「最近一周」按 5 个交易日计算", "板块口径为通达信概念板块"],
}
STOCK_HISTORY = {
    "version": 1,
    "shape": "stock_history",
    "target": {"mention": "茅台", "guess": "贵州茅台", "code": "600519.SH"},
    "event": {
        "preset_id": "breakout_ma_volume",
        "params": {"ma": 250, "volume_ratio": 2},
        "expr": "Cross($close, Mean($close,250)) & ($amount > Mean(Ref($amount,1),20)*2)",
        "label": "放量突破年线",
    },
    "time_range": {"from": "2016-01-01", "to": "2026-09-11"},
    "horizons": [5, 20, 60],
    "benchmark": "universe_equal_weight",
    "cost_bps": 30,
    "assumptions": [
        "「放量」理解为成交额 > 前 20 日均额的 2 倍",
        "「1周/1月/3月」= 5/20/60 个交易日",
    ],
}


def with_changes(base: dict, **changes) -> dict:
    return {**base, **changes}


def test_文档里的三个示例都能解析():
    assert isinstance(parse_spec(STOCK_LIST), StockListSpec)
    assert isinstance(parse_spec(BOARD_LIST), BoardListSpec)
    history = parse_spec(STOCK_HISTORY)
    assert isinstance(history, StockHistorySpec)
    assert history.time_range.start == date(2016, 1, 1)
    assert history.time_range.end == date(2026, 9, 11)


def test_回看区间读写都用from和to():
    history = parse_spec(STOCK_HISTORY)
    dumped = history.model_dump(mode="json", by_alias=True)
    assert dumped["time_range"] == {"from": "2016-01-01", "to": "2026-09-11"}
    assert parse_spec(dumped) == history


def test_没写的字段用默认值():
    spec = parse_spec({"shape": "stock_list", "as_of": "2026-09-11"})
    assert spec.limit == DEFAULTS["top_n"]
    assert spec.universe.base == "all_a"
    assert spec.universe.exclude == ("ST", "suspended", "new_listing_60d")
    assert spec.filter is None and spec.sort is None

    history = parse_spec(
        {
            "shape": "stock_history",
            "target": {"code": "600519.SH"},
            "event": {"expr": "$is_limit_up"},
            "time_range": {"from": "2020-01-01", "to": "2026-09-11"},
        }
    )
    assert history.horizons == (5, 20, 60)
    assert history.benchmark == "universe_equal_weight"
    assert history.cost_bps == 30


def test_持有天数去重并升序():
    spec = parse_spec(with_changes(STOCK_HISTORY, horizons=[60, 5, 20, 5]))
    assert spec.horizons == (5, 20, 60)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"horizons": []}, "至少要看一档"),
        ({"horizons": [0, 5]}, "1 ~ 250"),
        ({"horizons": [300]}, "1 ~ 250"),
        ({"horizons": list(range(1, 12))}, "最多 10 档"),
        ({"cost_bps": -1}, "greater than or equal"),
        ({"cost_bps": 501}, "less than or equal"),
        ({"benchmark": "index:000016.SH"}, "universe_equal_weight"),
        ({"time_range": {"from": "2026-09-11", "to": "2016-01-01"}}, "晚于终点"),
    ],
)
def test_个股回看的数值范围(changes, message):
    with pytest.raises(ValidationError, match=message):
        parse_spec(with_changes(STOCK_HISTORY, **changes))


@pytest.mark.parametrize("limit", [0, 501])
def test_取前N名的范围(limit):
    with pytest.raises(ValidationError, match="limit"):
        parse_spec(with_changes(STOCK_LIST, limit=limit))


def test_不认识的形状():
    with pytest.raises(ValidationError, match="shape"):
        parse_spec(with_changes(STOCK_LIST, shape="stock_table"))


def test_多写了不认识的字段直接报错():
    """LLM 把 limit 写成 top_n 这种笔误，不能悄悄忽略、用默认值顶上。"""
    with pytest.raises(ValidationError, match="top_n"):
        parse_spec(with_changes(STOCK_LIST, top_n=10))
    with pytest.raises(ValidationError, match="industy"):
        parse_spec(with_changes(STOCK_LIST, universe={"industy": "银行"}))


def test_股票池的板块只能是概念板块():
    with pytest.raises(ValidationError, match="concept"):
        parse_spec(
            with_changes(
                STOCK_LIST, universe={"board": {"type": "sw_industry", "code": "801780.SI"}}
            )
        )


def test_板块表的口径只有两种():
    with pytest.raises(ValidationError, match="board_type"):
        parse_spec(with_changes(BOARD_LIST, board_type="industry"))


def test_spec不可变():
    spec = parse_spec(STOCK_LIST)
    with pytest.raises(ValidationError):
        spec.limit = 10  # type: ignore[misc]


# ── 类型只收明确的写法 ──────────────────────────────────────────


@pytest.mark.parametrize("value", [1757548800, 20260911, "2026-09-11T00:00:00", "2026/09/11", True])
def test_日期只收YYYY_MM_DD_整数不当时间戳(value):
    with pytest.raises(ValidationError, match="as_of"):
        parse_spec(with_changes(STOCK_LIST, as_of=value))


@pytest.mark.parametrize("value", [True, "50", 50.5])
def test_整数不收布尔值_字符串_带小数的数(value):
    with pytest.raises(ValidationError, match="limit"):
        parse_spec(with_changes(STOCK_LIST, limit=value))


def test_没有小数部分的数当整数收():
    assert parse_spec(with_changes(STOCK_LIST, limit=50.0)).limit == 50
    assert parse_spec(with_changes(STOCK_HISTORY, horizons=[5.0, 20])).horizons == (5, 20)


def test_持有天数不收布尔值():
    with pytest.raises(ValidationError, match="horizons"):
        parse_spec(with_changes(STOCK_HISTORY, horizons=[5, True]))


@pytest.mark.parametrize("value", [True, "30", float("nan"), float("inf")])
def test_成本不收布尔值_字符串_NaN_无穷大(value):
    with pytest.raises(ValidationError, match="cost_bps"):
        parse_spec(with_changes(STOCK_HISTORY, cost_bps=value))


def test_回看区间的日期同样严格():
    with pytest.raises(ValidationError, match="from"):
        parse_spec(with_changes(STOCK_HISTORY, time_range={"from": 1735660800, "to": "2026-09-11"}))
