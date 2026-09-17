"""QuerySpec v2 的结构校验（DESIGN.md §1、§3）。不联网、不读数据。

要查数据的检查（代码在不在 scope 里、表达式合不合法）在 api/checks.py，不在这里。
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from litmus.spec.query import QuerySpec, parse_spec

MONIU = {"kind": "codes", "codes": ["002714.SZ"]}
PCT = {"name": "今年涨幅", "expr": "PctSince($close, 20251231)"}
PE = {"name": "市盈率", "expr": "$pe_ttm"}


def spec(**changes) -> dict:
    """一份能过的股票表 spec，按需覆盖。"""
    return {
        "scope": {"target": "stock", "industry": "农林牧渔"},
        "subject": {"kind": "pool"},
        "when": {"as_of": "2026-09-16"},
        "metrics": [PCT, PE],
        "output": {"kind": "table", "sort": {"by": "今年涨幅"}, "limit": 10},
        **changes,
    }


def card(**changes) -> dict:
    return spec(**{"subject": MONIU, "metrics": [PE], "output": {"kind": "card"}, **changes})


STUDY_OUTPUT = {
    "kind": "event_study",
    "event": {"preset_id": "limit_up", "expr": "$is_limit_up"},
}


def study(**changes) -> dict:
    return spec(
        **{
            "subject": MONIU,
            "when": {"range": {"from": "2016-01-04", "to": "2026-09-16"}},
            "metrics": [],
            "output": STUDY_OUTPUT,
            **changes,
        }
    )


def message(raw: dict) -> str:
    with pytest.raises(ValidationError) as caught:
        parse_spec(raw)
    return str(caught.value)


# ── 三种形态各自能过 ────────────────────────────────────────────


def test_三种形态都能解析():
    table = parse_spec(spec())
    assert table.version == 2 and table.output.kind == "table"
    assert table.scope.industry == "农林牧渔" and table.subject.kind == "pool"
    assert [m.name for m in table.metrics] == ["今年涨幅", "市盈率"]
    assert table.when.as_of == date(2026, 9, 16)

    assert parse_spec(card()).output.kind == "card"
    assert parse_spec(study()).output.kind == "event_study"


def test_什么都不填时的默认_算的范围是全A_看的是整个池子():
    bare = parse_spec({"output": {"kind": "table"}})
    assert (bare.scope.target, bare.scope.base) == ("stock", "all_a")
    assert bare.scope.exclude == ("ST", "suspended", "new_listing_60d")
    assert bare.subject.kind == "pool"
    assert bare.when.as_of is None and bare.when.range is None  # 由 api 回填
    assert bare.metrics == () and bare.narrate is False


def test_大模型只填原话_代码由api填():
    parsed = parse_spec(card(subject={"kind": "codes", "mentions": [{"mention": "牧原股份"}]}))
    assert parsed.subject.mentions[0].mention == "牧原股份"
    assert parsed.subject.codes == ()


# ── 非法组合：一条一个 ──────────────────────────────────────────


def test_组合1_卡要点名看谁():
    assert "卡要点名看谁" in message(card(subject={"kind": "pool"}))


def test_组合2_卡上要有指标():
    assert "卡上要有指标" in message(card(metrics=[]))


def test_组合3_统计不看展示指标():
    assert "不看展示指标" in message(study(metrics=[PE]))


def test_组合4_统计要一段区间():
    assert "要一段区间" in message(study(when={"as_of": "2026-09-16"}))


def test_组合5_表和卡看的是某一天():
    span = {"range": {"from": "2016-01-04", "to": "2026-09-16"}}
    assert "某一天" in message(spec(when=span))
    assert "某一天" in message(card(when=span))


def test_组合6_聚合只能配卡():
    aggregate = {"kind": "aggregate"}
    assert "只能是 card" in message(spec(subject=aggregate))
    assert "只能是 card" in message(study(subject=aggregate))


def test_组合7_点名了要说是谁():
    assert "看的是谁" in message(card(subject={"kind": "codes"}))


def test_组合8_总结只跟着卡走():
    assert "总结只跟着卡走" in message(spec(narrate=True))
    assert parse_spec(card(narrate=True)).narrate is True  # 卡可以


def test_组合9_排序要指一个指标_并列出可选的():
    text = message(spec(output={"kind": "table", "sort": {"by": "市净率"}}))
    assert "sort.by 要填一个指标的名字" in text
    assert "今年涨幅、市盈率" in text  # 把可选的列出来
    # 写成公式也不行：公式只在 metrics 里定义一次
    assert "sort.by" in message(spec(output={"kind": "table", "sort": {"by": "$pe_ttm"}}))


def test_组合10_指标不能重名():
    same = {"name": "今年涨幅", "expr": "$pct_chg"}
    assert "指标重名了：今年涨幅" in message(spec(metrics=[PCT, same]))


def test_卡上不许有筛选排序取前N_多填的字段直接拦掉():
    for extra in ({"sort": {"by": "市盈率"}}, {"limit": 10}, {"filter": {"expr": "$pe_ttm < 20"}}):
        assert "extra" in message(card(output={"kind": "card", **extra})).lower()


# ── 时间与数值的边界 ────────────────────────────────────────────


def test_时点和区间只能填一个():
    both = {"as_of": "2026-09-16", "range": {"from": "2016-01-04", "to": "2026-09-16"}}
    assert "只能填一个" in message(spec(when=both))


def test_区间起点不能晚于终点():
    bad = {"range": {"from": "2026-09-16", "to": "2016-01-04"}}
    assert "晚于终点" in message(study(when=bad))


def test_日期只收YYYY_MM_DD_不把整数当时间戳():
    assert "YYYY-MM-DD" in message(spec(when={"as_of": 1757548800}))
    assert "YYYY-MM-DD" in message(spec(when={"as_of": "2026/09/16"}))


def test_取前N的范围():
    assert parse_spec(spec(output={"kind": "table", "limit": 500})).output.limit == 500
    assert "500" in message(spec(output={"kind": "table", "limit": 501}))
    assert message(spec(output={"kind": "table", "limit": 0}))
    assert "整数" in message(spec(output={"kind": "table", "limit": True}))


def test_持有天数去重升序_成本有上限():
    parsed = parse_spec(study(output={**STUDY_OUTPUT, "horizons": [20, 5, 5]}))
    assert parsed.output.horizons == (5, 20)
    assert "1 ~ 250" in message(study(output={**STUDY_OUTPUT, "horizons": [999]}))
    assert "至少要看一档" in message(study(output={**STUDY_OUTPUT, "horizons": []}))
    assert message(study(output={**STUDY_OUTPUT, "cost_bps": 501}))


def test_不认识的字段直接报错_不悄悄忽略():
    assert "extra" in message(spec(top_n=10)).lower()
    assert "extra" in message(spec(scope={"target": "stock", "行业": "电子"})).lower()


def test_确认卡的说明和默认值标记由代码填_请求里带来的能被覆盖():
    # 结构上收得下，但 api 会用自己算的那份覆盖
    parsed = parse_spec(spec(defaults_used=["scope.exclude"], assumptions=["随便写的"]))
    assert parsed.defaults_used == ("scope.exclude",)


def test_版本号写错报错():
    assert message(spec(version=1))


def test_spec是不可变的():
    parsed: QuerySpec = parse_spec(spec())
    with pytest.raises(ValidationError):
        parsed.narrate = True  # type: ignore[misc]
