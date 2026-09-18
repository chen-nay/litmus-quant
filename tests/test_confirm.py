"""确认卡文案（DESIGN.md §1）。不联网、不读数据——Facts 手写。"""

from __future__ import annotations

from datetime import date

from litmus.spec.confirm import (
    AFTER,
    EVENT,
    HOW,
    WHAT,
    WHEN,
    WHO,
    Facts,
    Mention,
    render_confirm,
    summarize,
)
from litmus.spec.query import parse_spec

TABLE = {
    "scope": {"target": "stock", "industry": "农林牧渔"},
    "subject": {"kind": "pool"},
    "when": {"as_of": "2026-09-16"},
    "metrics": [{"name": "今年以来涨幅", "expr": "PctSince($close, 20251231)"}],
    "output": {"kind": "table", "sort": {"by": "今年以来涨幅"}, "limit": 10},
    "defaults_used": ["when.as_of", "scope.base", "scope.exclude"],
}
TABLE_FACTS = Facts(
    metric_texts={"今年以来涨幅": "收盘价较 2025-12-31 的涨跌幅"},
    pool_size=116,
    industry_scope="申万一级行业",
    mentions=(Mention("今年以来涨幅", "metrics.今年以来涨幅"), Mention("前 10", "output.limit")),
    data_dates=(("股票行情", date(2026, 9, 16)),),
)

STUDY = {
    "scope": {"target": "stock"},
    "subject": {"kind": "codes", "codes": ["600519.SH"]},
    "when": {"range": {"from": "2016-01-04", "to": "2026-09-16"}},
    "output": {
        "kind": "event_study",
        "event": {
            "preset_id": "breakout_ma_volume",
            "expr": "Cross($close, Mean($close,250))",
            "label": "放量突破 250 日均线（成交额超过前 20 日均额的 2 倍）",
        },
        "horizons": [5, 20, 60],
    },
    "defaults_used": ["output.horizons", "output.benchmark", "output.cost_bps"],
}
STUDY_FACTS = Facts(
    names={"600519.SH": "贵州茅台"},
    first_date=date(2017, 1, 12),
    mentions=(Mention("茅台", "subject"), Mention("放量突破年线", "output.event")),
    data_dates=(("股票行情", date(2026, 9, 16)),),
)


def lines(raw: dict, facts: Facts) -> dict[str, list[str]]:
    """渲染成 {小标题: [那一组的每一行文字]}。不属于任何一组的挂在 ""。"""
    grouped: dict[str, list[str]] = {}
    for item in render_confirm(parse_spec(raw), facts).items:
        grouped.setdefault(item.group, []).append(item.text)
    return grouped


def defaults(raw: dict, facts: Facts) -> list[str]:
    return [i.text for i in render_confirm(parse_spec(raw), facts).items if i.default]


# ── 一句话总结 ──────────────────────────────────────────────────


def test_总结_表():
    assert summarize(parse_spec(TABLE), TABLE_FACTS) == (
        "农林牧渔里，按「今年以来涨幅」从高到低取前 10 只股票"
    )


def test_总结_表_有筛选时把筛选的说法带上():
    raw = {
        **TABLE,
        "output": {
            "kind": "table",
            "filter": {"expr": "$pe_ttm < 40", "label": "市盈率低于 40 倍"},
            "sort": {"by": "今年以来涨幅"},
            "limit": 10,
        },
    }
    assert "市盈率低于 40 倍的" in summarize(parse_spec(raw), TABLE_FACTS)


def test_总结_表_从低到高():
    raw = {**TABLE, "output": {"kind": "table", "sort": {"by": "今年以来涨幅", "order": "asc"}}}
    assert "从低到高" in summarize(parse_spec(raw), TABLE_FACTS)


def test_总结_板块表不重复量词():
    raw = {
        "scope": {"target": "concept"},
        "subject": {"kind": "pool"},
        "when": {"as_of": "2026-09-16"},
        "metrics": [{"name": "10日涨停家数", "expr": "Sum($limit_up_num, 10)"}],
        "output": {"kind": "table", "sort": {"by": "10日涨停家数"}, "limit": 5},
    }
    assert summarize(parse_spec(raw)) == "通达信概念板块里，按「10日涨停家数」从高到低取前 5 个"


def test_总结_统计_写事件全称():
    text = summarize(parse_spec(STUDY), STUDY_FACTS)
    assert text.startswith(
        "贵州茅台历史上每次放量突破 250 日均线（成交额超过前 20 日均额的 2 倍）之后"
    )
    assert "接下来 5、20、60 个交易日涨跌多少" in text


def test_总结_卡():
    raw = {
        "subject": {"kind": "codes", "codes": ["002714.SZ"]},
        "when": {"as_of": "2026-09-16"},
        "metrics": [
            {"name": "市盈率TTM", "expr": "$pe_ttm"},
            {"name": "两年分位", "expr": "TsRank($pe_ttm, 500)"},
        ],
        "output": {"kind": "card"},
    }
    facts = Facts(names={"002714.SZ": "牧原股份"})
    assert summarize(parse_spec(raw), facts) == "牧原股份的市盈率TTM、两年分位"


# ── 分组 ────────────────────────────────────────────────────────


def test_表分成四组_数据截至不属于任何一组():
    grouped = lines(TABLE, TABLE_FACTS)
    assert list(grouped) == [WHO, WHEN, WHAT, HOW, ""]
    assert grouped[""] == ["本地数据截至：股票行情 2026-09-16"]


def test_统计的组是看谁看哪天什么事件怎么算_没有看哪些数():
    grouped = lines(STUDY, STUDY_FACTS)
    assert list(grouped) == [WHO, WHEN, EVENT, AFTER, ""]
    assert WHAT not in grouped  # 统计不看展示指标


def test_看谁这一组_算的范围和池子大小都在():
    who = lines(TABLE, TABLE_FACTS)[WHO]
    assert who[0] == "算的范围：农林牧渔（申万一级行业，116 只）"
    assert "按每个交易日当时的归属取成分" in who
    assert "股票池：沪深A股（不含北交所）" in who
    assert "剔除：ST / *ST、停牌、上市不满 60 个交易日" in who


def test_点名看谁时_算的范围仍然写出来_排名在这里面排():
    """「牧原在农林牧渔里排第几」：看的是一只，算的范围是整个行业。"""
    raw = {
        "scope": {"target": "stock", "industry": "农林牧渔"},
        "subject": {"kind": "codes", "codes": ["002714.SZ"]},
        "when": {"range": {"from": "2016-01-04", "to": "2026-09-16"}},
        "output": {
            "kind": "event_study",
            "event": {"preset_id": "limit_up", "expr": "$is_limit_up", "label": "涨停"},
        },
    }
    facts = Facts(
        names={"002714.SZ": "牧原股份"},
        pool_size=116,
        industry_scope="申万一级行业",
        uses_rank=True,
    )
    grouped = lines(raw, facts)
    assert grouped[WHO][0] == "股票：牧原股份（002714.SZ）"
    assert grouped[WHO][1] == "算的范围：农林牧渔（申万一级行业，116 只）"


def test_统计的同期对照是算的范围的等权平均():
    raw = {**STUDY, "scope": {"target": "stock", "industry": "农林牧渔", "exclude": ["ST"]}}
    after = lines(raw, STUDY_FACTS)[AFTER]
    assert "同期对照：买入日农林牧渔等权平均（剔除 ST / *ST，持有期内退市的按最后价格算）" in after


# ── 卡底下的「怎么算的」 ────────────────────────────────────────

CARD = {
    "scope": {"target": "stock", "industry": "农林牧渔"},
    "subject": {"kind": "codes", "codes": ["002714.SZ"]},
    "when": {"as_of": "2026-09-16"},
    "metrics": [{"name": "今年以来涨幅排名", "expr": "Rank(PctSince($close, 20251231))"}],
    "output": {"kind": "card"},
    "defaults_used": ["when.as_of", "scope.base", "scope.exclude"],
}
CARD_FACTS = Facts(
    metric_texts={
        "今年以来涨幅排名": "按收盘价从 2025-12-31 到当天的涨跌幅，在算的范围里从高到低排名"
    },
    names={"002714.SZ": "牧原股份"},
    pool_size=100,
    industry_scope="申万一级行业",
    uses_rank=True,
    lookback=180,
    data_dates=(("股票行情", date(2026, 9, 16)),),
)


def test_卡不写看谁_标题上就是它():
    grouped = lines(CARD, CARD_FACTS)
    assert not any(text.startswith("股票：") for group in grouped.values() for text in group)


def test_卡排名时写出算的范围_怎么排写在指标那一行():
    grouped = lines(CARD, CARD_FACTS)
    assert grouped[WHO] == [
        "算的范围：农林牧渔（申万一级行业，100 只）",
        "按每个交易日当时的归属取成分",
        "股票池：沪深A股（不含北交所）",
        "剔除：ST / *ST、停牌、上市不满 60 个交易日",
    ]
    assert grouped[WHAT] == [
        "今年以来涨幅排名：按收盘价从 2025-12-31 到当天的涨跌幅，在算的范围里从高到低排名"
    ]


def test_卡不排名时不写算的范围_池子影响不到卡上的数():
    raw = {**CARD, "metrics": [{"name": "市净率", "expr": "$pb"}]}
    facts = Facts(metric_texts={"市净率": "市净率在近 500 日里的分位"}, pool_size=100)
    assert WHO not in lines(raw, facts)


def test_卡的涨跌换成和指数比时写出来():
    raw = {**CARD, "output": {"kind": "card", "benchmark": "index:000300.SH"}}
    assert "涨跌的同期对照：沪深300 指数" in lines(raw, CARD_FACTS)[WHAT]
    assert not any("同期对照" in text for text in lines(CARD, CARD_FACTS)[WHAT])


def test_卡上指标的中文和名字一样时不重复写():
    raw = {**CARD, "metrics": [{"name": "市净率", "expr": "$pb"}]}
    facts = Facts(metric_texts={"市净率": "市净率"})
    assert WHAT not in lines(raw, facts)


def test_条件用到上市时间_又默认剔除了次新股_说出来():
    raw = {
        **TABLE,
        "output": {
            "kind": "table",
            "filter": {"expr": "$list_days <= 30"},
            "sort": {"by": "今年以来涨幅"},
        },
    }
    facts = Facts(pool_size=5000, uses_listing=True)
    assert (
        "条件里用到了上市时间，但上市不满 60 个交易日的股票已经剔除了：要看次新股，把这一项去掉"
        in lines(raw, facts)[WHO]
    )
    kept = {**raw, "scope": {"target": "stock", "exclude": ["ST", "suspended"]}}
    assert not any("上市时间" in text for text in lines(kept, facts)[WHO])


def test_看哪天_回看区间写出实际起点():
    assert lines(STUDY, STUDY_FACTS)[WHEN] == [
        "回看区间：2016-01-04 ~ 2026-09-16，实际从 2017-01-12 算起（裁到本地数据、扣掉预热期之后）"
    ]


def test_板块表写明数据区间和只含现存的():
    raw = {
        "scope": {"target": "concept"},
        "subject": {"kind": "pool"},
        "when": {"as_of": "2026-09-16"},
        "metrics": [{"name": "10日涨停家数", "expr": "Sum($limit_up_num, 10)"}],
        "output": {"kind": "table", "sort": {"by": "10日涨停家数"}, "limit": 5},
    }
    facts = Facts(pool_size=269, board_range=(date(2025, 3, 28), date(2026, 9, 16)))
    grouped = lines(raw, facts)
    assert grouped[WHO] == [
        "算的范围：通达信概念板块，269 个",
        "只含现存的板块，已经撤销的不在里面",
    ]
    assert "这类数据从 2025-03-28 起，问更早的答不了" in grouped[WHEN]


def test_怎么出_没筛选和没排序都说出来():
    grouped = lines({**TABLE, "output": {"kind": "table"}}, TABLE_FACTS)
    assert grouped[HOW][:2] == ["不筛选", "没有指定排序，按成交额从高到低排"]


# ── 原话与默认值 ────────────────────────────────────────────────


def test_有原话时写成理解为_没有就写名称冒号():
    grouped = lines(TABLE, TABLE_FACTS)
    assert grouped[WHAT] == ["「今年以来涨幅」理解为：收盘价较 2025-12-31 的涨跌幅"]
    assert "「前 10」理解为：10 名" in grouped[HOW]
    # 没给原话的栏目
    assert "日期：2026-09-16" in grouped[WHEN]


def test_原话就是值本身时不写理解为():
    facts = Facts(
        pool_size=116,
        industry_scope="申万一级行业",
        mentions=(Mention("农林牧渔", "scope.industry"),),
    )
    assert lines(TABLE, facts)[WHO][0] == "算的范围：农林牧渔（申万一级行业，116 只）"


def test_默认值标出来():
    marked = defaults(TABLE, TABLE_FACTS)
    assert "日期：2026-09-16" in marked
    assert "股票池：沪深A股（不含北交所）" in marked
    assert "剔除：ST / *ST、停牌、上市不满 60 个交易日" in marked
    assert "「今年以来涨幅」理解为：收盘价较 2025-12-31 的涨跌幅" not in marked


def test_用了默认门槛的筛选条件也标默认值():
    raw = {
        **TABLE,
        "output": {
            "kind": "table",
            "filter": {"expr": "$market_cap < 30亿"},
            "sort": {"by": "今年以来涨幅"},
        },
        "defaults_used": [],
    }
    facts = Facts(
        filter_text="总市值 < 30亿",
        defaulted=frozenset({"output.filter"}),
        **{k: v for k, v in vars(TABLE_FACTS).items() if k not in ("filter_text", "defaulted")},
    )
    assert "先筛：总市值 < 30亿" in defaults(raw, facts)


def test_怎么算这一组_四条都在且默认值标对():
    after = lines(STUDY, STUDY_FACTS)[AFTER]
    assert after[0] == "持有天数：5、20、60 个交易日，从买入日起算"
    assert "买入价是触发日下一个交易日的开盘价" in after[1]
    assert after[2] == (
        "同期对照：买入日全A等权平均（剔除 ST / *ST、停牌、上市不满 60 个交易日，持有期内退市的按最后价格算）"
    )
    assert after[3].startswith("交易成本：0.30%，买卖双边合计")
    assert all(text in defaults(STUDY, STUDY_FACTS) for text in (after[0], after[2], after[3]))


def test_什么事件这一组():
    event = lines(STUDY, STUDY_FACTS)[EVENT]
    assert event[0] == (
        "「放量突破年线」理解为：放量突破 250 日均线（成交额超过前 20 日均额的 2 倍）"
    )
    assert event[1] == "只算由不满足变为满足的那一天，连续成立不重复计"


def test_预热期长时提醒():
    facts = Facts(lookback=499, **{k: v for k, v in vars(TABLE_FACTS).items() if k != "lookback"})
    assert any("往前读 499 个交易日" in text for text in lines(TABLE, facts)[WHAT])


def test_没有Facts时也能渲染_表达式当说明用():
    grouped = lines(TABLE, Facts())
    assert grouped[WHAT] == ["今年以来涨幅：PctSince($close, 20251231)"]
    assert grouped[WHO][0] == "算的范围：农林牧渔"
