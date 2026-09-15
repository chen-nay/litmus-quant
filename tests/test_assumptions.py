"""确认卡说明文字的模板（spec.render_assumptions）：不读数据，Facts 手写。"""

from __future__ import annotations

from datetime import date

from litmus.spec import Assumption, Facts, Mention, parse_spec, render_assumptions


def rows(items: list[Assumption]) -> list[tuple[str | None, str, bool]]:
    return [(item.field, item.text, item.default) for item in items]


def stock_list(**extra):
    return parse_spec(
        {
            "shape": "stock_list",
            "as_of": "2026-09-14",
            "filter": {"expr": "$pct_chg > 9", "label": "涨幅超过 9%"},
            "sort": {"by": "$amount"},
            "limit": 10,
            "universe": {"base": "all_a", "exclude": ["ST", "suspended"]},
            **extra,
        }
    )


FACTS = Facts(expressions={"filter.expr": "当日涨跌幅 > 9", "sort.by": "成交额"})


def history(**extra):
    return parse_spec(
        {
            "shape": "stock_history",
            "target": {"code": "600519.SH"},
            "event": {
                "preset_id": "breakout_ma",
                "params": {"ma": 250},
                "expr": "Cross($close, Mean($close, 250))",
                "label": "突破 250 日均线",
            },
            "time_range": {"from": "2016-01-01", "to": "2025-12-31"},
            "horizons": [5, 20],
            **extra,
        }
    )


def test_股票表_逐栏说明_默认值标出来():
    spec = stock_list(defaults_used=["universe.exclude"])
    assert rows(render_assumptions(spec, FACTS)) == [
        ("as_of", "日期：2026-09-14", False),
        ("filter", "筛选条件：涨幅超过 9%（当日涨跌幅 > 9）", False),
        ("sort", "排序：成交额，从高到低", False),
        ("limit", "取前：10 名", False),
        ("universe.base", "股票池：沪深A股（不含北交所）", False),
        ("universe.exclude", "剔除：ST / *ST、停牌", True),
    ]


def test_用户原话的说法_理解为栏目里的定义():
    facts = Facts(
        expressions=FACTS.expressions,
        mentions=(Mention("昨天", "as_of"), Mention("涨停", "filter.expr")),
    )
    texts = [item.text for item in render_assumptions(stock_list(), facts)]
    assert texts[:2] == [
        "「昨天」理解为：2026-09-14",
        "「涨停」理解为：当日涨跌幅 > 9",  # 有原话时不再重复条件名称
    ]


def test_原话就是说明开头的名称_不写理解为():
    board = parse_spec({"shape": "board_list", "board_type": "sw_industry", "as_of": "2026-09-14"})
    facts = Facts(
        board_range=(date(2016, 1, 4), date(2026, 9, 14)),
        mentions=(Mention("申万一级行业", "board_type"),),
    )
    assert render_assumptions(board, facts)[0].text == (
        "板块口径：申万一级行业，数据从 2016-01-04 到 2026-09-14"
    )

    def target(mention: str) -> str:
        facts = Facts(stock_name="贵州茅台", mentions=(Mention(mention, "target"),))
        return render_assumptions(history(), facts)[0].text

    assert target("贵州茅台") == "股票：贵州茅台（600519.SH）"
    assert target("茅台") == "「茅台」理解为：贵州茅台（600519.SH）"


def test_没指定排序_概念板块成分_排名_长窗口都有说明():
    spec = stock_list(
        as_of="2025-06-03",
        sort=None,
        universe={"board": {"type": "concept", "code": "880728.TDX"}},
        defaults_used=["sort"],
    )
    facts = Facts(
        expressions={"filter.expr": "当日涨跌幅 > 9"},
        uses_rank=True,
        lookback=249,
        board_name="航运概念",
        concept_snapshot=date(2026, 9, 11),
    )
    items = rows(render_assumptions(spec, facts))
    assert ("sort", "排序：成交额从高到低（没有指定排序）", True) in items
    assert ("universe.board", "概念板块：航运概念", False) in items
    notes = [text for field, text, _ in items if field is None]
    assert notes == [
        "「航运概念」只有 2026-09-11 的成分：查 2025-06-03 时，之后才调入的股票也算在内，当时在、后来调出的不会出现",
        "排名（Rank）是在当天的股票池（已按上面的范围和剔除项过滤）里排的分位，最高为 1",
        "「N 日」都按交易日数，停牌的日子不算",
        "条件要往前读 249 个交易日的行情：上市不满这么久的股票算不出来，不会出现在结果里",
    ]


def test_板块表_口径带数据区间_概念板块只含现存的():
    spec = parse_spec({"shape": "board_list", "board_type": "concept", "as_of": "2026-09-14"})
    facts = Facts(board_range=(date(2025, 3, 28), date(2026, 9, 14)))
    first = render_assumptions(spec, facts)[0]
    assert (first.field, first.text) == (
        "board_type",
        "板块口径：通达信概念板块，数据从 2025-03-28 到 2026-09-14，只含现在还在的板块",
    )


def test_个股回看():
    spec = history(defaults_used=["benchmark", "cost_bps", "event.params.ma"])
    facts = Facts(stock_name="贵州茅台", first_date=date(2017, 1, 5))
    assert rows(render_assumptions(spec, facts)) == [
        ("target", "股票：贵州茅台（600519.SH）", False),
        ("event", "事件：突破 250 日均线", True),
        (None, "事件只算由不满足变为满足的那一天，连续成立不重复计", False),
        (
            "time_range",
            "回看区间：2016-01-01 ~ 2025-12-31，实际从 2017-01-05 算起（裁到本地数据、扣掉预热期之后）",
            False,
        ),
        ("horizons", "持有天数：5、20 个交易日，从买入日起算", False),
        (
            None,
            "买入价是触发日下一个交易日的开盘价，开盘涨停或停牌就往后顺延；卖出价是持有期最后一天的收盘价，跌停或停牌就往后顺延",
            False,
        ),
        (
            "benchmark",
            "同期对照：买入日全A等权平均（剔除 ST、停牌、次新股，持有期内退市的按最后价格算）",
            True,
        ),
        (
            "cost_bps",
            "交易成本：0.30%，买卖双边合计；平均涨跌不扣成本，另外单列扣掉成本后的数",
            True,
        ),
    ]


def test_改了参数说明跟着变():
    before = render_assumptions(history(horizons=[5, 20]))
    after = render_assumptions(history(horizons=[10], cost_bps=50))
    changed = {a.text for a in after} - {b.text for b in before}
    assert changed == {
        "持有天数：10 个交易日，从买入日起算",
        "交易成本：0.50%，买卖双边合计；平均涨跌不扣成本，另外单列扣掉成本后的数",
    }
