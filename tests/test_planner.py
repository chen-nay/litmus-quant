"""llm.plan() 的离线测试：假的大模型客户端，逐条核对转换、防线②的重试、澄清、改写建议。不发请求。"""

from __future__ import annotations

from datetime import date

from litmus.llm import (
    CLARIFY,
    FAILED,
    OK,
    UNSUPPORTED,
    LLMClient,
    LLMError,
    LLMFormatError,
    NameMention,
    PlanContext,
    PreviousTurn,
    Question,
    StructuredReply,
    plan,
)
from litmus.llm.planner import clean_alternatives, system_variables
from litmus.llm.prompts import all_prompts, load_prompt
from litmus.signals import load_events
from litmus.spec import Mention

EVENTS = load_events()
CONTEXT = PlanContext(
    today=date(2026, 9, 15),
    latest_trading_day=date(2026, 9, 14),
    history_from=date(2016, 1, 4),
    targets=("stock", "sw_industry", "concept"),
    industries=("银行", "电子", "食品饮料"),
)


class FakeClient(LLMClient):
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str]] = []

    def structured(self, system, user, schema):
        self.calls.append((system, user))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return StructuredReply(outcome, 0.1)


def ask(*outcomes, query="问题", previous=None, revise=None, context=CONTEXT):
    client = FakeClient(*outcomes)
    return plan(query, context, client, EVENTS, previous, revise), client


TABLE = {
    "status": "ok",
    "subject": {"kind": "pool"},
    "when": {"as_of": "2026-09-14"},
    "metrics": [{"name": "放大倍数", "expr": "$amount / Ref(Mean($amount, 5), 1)"}],
    "output": {
        "kind": "table",
        "filter": {"expr": "$amount > 1.4 * Ref(Mean($amount, 5), 1)", "label": "放量"},
        "sort": {"by": "放大倍数"},
        "limit": 20,
    },
}

CARD = {
    "status": "ok",
    "subject": {"kind": "codes", "mentions": [{"mention": "牧原", "guess": "牧原股份"}]},
    "metrics": [
        {"name": "市盈率TTM", "expr": "$pe_ttm"},
        {"name": "市盈率两年分位", "expr": "TsRank($pe_ttm, 500)"},
    ],
    "output": {"kind": "card"},
    "narrate": True,
}

STUDY = {
    "status": "ok",
    "subject": {"kind": "codes", "mentions": [{"mention": "茅台", "guess": "贵州茅台"}]},
    "output": {
        "kind": "event_study",
        "event": {"preset_id": "breakout_ma_volume", "params": {"ma": 250}},
    },
}


def with_output(draft: dict, **changes) -> dict:
    return {**draft, "output": {**draft["output"], **changes}}


# ── 提示词 ──────────────────────────────────────────────────────


def test_提示词文件都能加载_系统提示词的变量都有代码填():
    assert {prompt.id for prompt in all_prompts()} == {
        "planner.system",
        "planner.repair",
        "planner.followup",
        "planner.revise",
    }
    text = load_prompt("planner.system").render(**system_variables(CONTEXT, EVENTS))
    for expected in (
        "$amount 成交额（元）",
        "breakout_ma 突破均线",
        "银行、电子、食品饮料",
        "2026-09-14",
        "「小市值」没给数字：总市值低于 30亿，写成 $market_cap < 30亿",
    ):
        assert expected in text


def test_日期换算表_交易日数由代码按本地日历数好():
    days = (
        date(2025, 12, 30),
        date(2025, 12, 31),
        date(2026, 1, 5),
        date(2026, 6, 30),
        date(2026, 7, 1),
        date(2026, 8, 31),
        date(2026, 9, 1),
        date(2026, 9, 11),
        date(2026, 9, 14),
    )
    context = PlanContext(**{**CONTEXT.__dict__, "trading_days": days})  # 今天 09-15 星期二
    text = system_variables(context, EVENTS)["dates"]
    assert "今天：2026-09-15（星期二）" in text
    assert "最近 10 个交易日，从近到远：2026-09-14（一）、2026-09-11（五）" in text
    assert (
        "本周以来：PctSince($close, 20260911)（和 2026-09-11 收盘比，到 2026-09-14 共 1 个交易日）"
        in text
    )
    assert "本月以来：PctSince($close, 20260831)" in text
    assert (
        "本季度以来：PctSince($close, 20260630)（和 2026-06-30 收盘比，到 2026-09-14 共 5 个交易日）"
        in text
    )
    assert (
        "今年以来：PctSince($close, 20251231)（和 2025-12-31 收盘比，到 2026-09-14 共 7 个交易日）"
        in text
    )

    # 一周没同步：本地还没有这周的数据，不拿旧数据凑
    stale = PlanContext(**{**context.__dict__, "today": date(2026, 9, 22)})
    assert (
        "本周以来：本地数据截至 2026-09-14，还没有这段的行情"
        in system_variables(stale, EVENTS)["dates"]
    )


def test_卡的默认指标组_今年以来的起点按本地日历填():
    days = (date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 5), date(2026, 9, 14))
    context = PlanContext(**{**CONTEXT.__dict__, "trading_days": days})
    variables = system_variables(context, EVENTS)
    assert variables["since_new_year"] == "20251231"
    assert "- 今年以来涨跌：PctSince($close, 20251231)" in variables["default_metrics"]
    assert "- 市盈率两年分位：TsRank($pe_ttm, 500)" in variables["default_metrics"]


def test_概念板块不可用时_提示词里说清楚():
    context = PlanContext(**{**CONTEXT.__dict__, "targets": ("stock", "sw_industry")})
    assert "通达信概念板块当前不可用" in system_variables(context, EVENTS)["board_fields"]


# ── ok：转成查询条件 ────────────────────────────────────────────


def test_表_转成查询条件_没说的栏目不填_不认识的栏目丢掉():
    output = {
        **TABLE,
        "mentions": [
            {"phrase": "昨天", "field": "when.as_of"},
            {"phrase": "放大倍数", "field": "metrics.放大倍数"},
            {"phrase": "茅台", "field": "股票"},
            {"phrase": "涨幅", "field": "metrics.不存在的指标"},
        ],
        "message": "ok 时不该有",
        "alternatives": ["ok 时不该有"],
    }
    result, client = ask(output)
    assert result.status == OK and result.attempts == 1 and len(client.calls) == 1
    assert result.spec == {
        "subject": {"kind": "pool"},
        "when": {"as_of": "2026-09-14"},
        "metrics": TABLE["metrics"],
        "output": {**TABLE["output"], "sort": {"by": "放大倍数", "order": "desc"}},
    }
    assert result.mentions == (
        Mention("昨天", "when.as_of"),
        Mention("放大倍数", "metrics.放大倍数"),
    )
    assert (result.message, result.alternatives) == ("", ())
    assert len(result.prompt_version) == 8


def test_漏填status但填了output_当成ok():
    output = {key: value for key, value in TABLE.items() if key != "status"}
    assert ask(output)[0].status == OK


def test_卡_点名的标的只给原话和猜测名_总结的开关带过去():
    result, _ = ask(CARD)
    assert result.status == OK
    assert result.spec == {
        "subject": {"kind": "codes"},
        "metrics": CARD["metrics"],
        "output": {"kind": "card"},
        "narrate": True,
    }
    assert result.subjects == (NameMention("牧原", "牧原股份"),)


def test_卡_点名几个就几个_重复的只留一个():
    mentions = [
        {"mention": "牧原", "guess": "牧原股份"},
        {"mention": "温氏", "guess": "温氏股份"},
        {"mention": "牧原", "guess": "牧原股份"},
    ]
    result, _ = ask({**CARD, "subject": {"kind": "codes", "mentions": mentions}})
    assert result.subjects == (NameMention("牧原", "牧原股份"), NameMention("温氏", "温氏股份"))


def test_卡_点名的是板块时按板块类型查():
    draft = {
        **CARD,
        "scope": {"target": "sw_industry"},
        "subject": {"kind": "codes", "mentions": [{"mention": "银行", "guess": "银行"}]},
        "metrics": [{"name": "近 20 日涨幅排名", "expr": "Rank(Pct($close, 20))"}],
    }
    result, _ = ask(draft)
    assert result.status == OK and result.spec["scope"] == {"target": "sw_industry"}
    assert result.subjects == (NameMention("银行", "银行"),)


def test_统计_股票只给原话和猜测_只留用户说到的参数():
    result, _ = ask(STUDY)
    assert result.status == OK
    assert result.spec == {"subject": {"kind": "codes"}, "output": STUDY["output"]}
    assert result.subjects == (NameMention("茅台", "贵州茅台"),)


def test_统计只说了起点_终点用本地数据的最后一天():
    result, _ = ask({**STUDY, "when": {"range": {"from": "2020-01-01"}}})
    assert result.spec["when"] == {"range": {"from": "2020-01-01", "to": "2026-09-14"}}


def test_要看次新股_剔除项照大模型填的():
    draft = {**TABLE, "scope": {"exclude": ["ST", "suspended"]}}
    assert ask(draft)[0].spec["scope"] == {"exclude": ["ST", "suspended"]}


def test_卡的同期对照只能换成宽基指数():
    result, _ = ask(with_output(CARD, benchmark="index:000300.SH"))
    assert result.status == OK and result.spec["output"] == {
        "kind": "card",
        "benchmark": "index:000300.SH",
    }

    result, client = ask(with_output(CARD, benchmark="universe_equal_weight"), CARD)
    assert result.status == OK and "卡的 output.benchmark 只能是" in client.calls[1][1]


def test_限定板块只给原话和猜测名():
    draft = {**TABLE, "scope": {"board": {"mention": "光模块", "guess": "光通信"}}}
    result, _ = ask(draft)
    assert result.board == NameMention("光模块", "光通信")
    assert "scope" not in result.spec


# ── 防线②：不过就带着问题重试一次 ──────────────────────────────


def test_表达式写错_带着问题重试一次():
    bad = with_output(TABLE, filter={"expr": "amount[-1] > 2"})
    result, client = ask(bad, TABLE, query="昨天放量的股票")
    assert (result.status, result.attempts) == (OK, 2)
    repair = client.calls[1][1]
    assert "上一次的输出没有通过检查" in repair and "昨天放量的股票" in repair
    assert "筛选条件「amount[-1] > 2」" in repair


def test_重试后还不对_返回failed并说明原因():
    bad = {**TABLE, "metrics": [{"name": "放大倍数", "expr": "$no_such_field"}]}
    result, client = ask(bad, bad)
    assert result.status == FAILED and len(client.calls) == 2
    assert "指标「放大倍数」" in result.error


def test_排序填了公式不是指标名_重试():
    bad = with_output(TABLE, sort={"by": "$amount / Ref(Mean($amount, 5), 1)"})
    result, client = ask(bad, TABLE)
    assert result.status == OK
    assert "sort.by 要填 metrics 里某一项的 name，不是公式" in client.calls[1][1]
    assert "可选：放大倍数" in client.calls[1][1]


def test_按条件真假排序_重试():
    bad = {**TABLE, "metrics": [{"name": "放大倍数", "expr": "$amount > 1亿"}]}
    result, client = ask(bad, TABLE)
    assert result.status == OK and "排序依据要是数值" in client.calls[1][1]


def test_卡带了排序_重试():
    """2026-09-17 实测：大模型给卡也填了 sort。"""
    bad = with_output(CARD, sort={"by": "市盈率TTM"}, limit=1)
    result, client = ask(bad, CARD)
    assert result.status == OK and "卡不筛不排，output 里不要填 sort、limit" in client.calls[1][1]


def test_猜的名字填成代码_重试():
    """2026-09-17 实测：guess 被填成 002714、002714.SZ。"""
    for code in ("002714", "002714.SZ"):
        bad = {
            **CARD,
            "subject": {"kind": "codes", "mentions": [{"mention": "牧原", "guess": code}]},
        }
        result, client = ask(bad, CARD)
        assert result.status == OK and "guess 要填你猜的全称，不要填代码" in client.calls[1][1]


def test_点名看谁却没说是谁_重试():
    bad = {**CARD, "subject": {"kind": "codes"}}
    result, client = ask(bad, CARD)
    assert (
        result.status == OK and "subject.mentions 要填用户原话里的股票或板块" in client.calls[1][1]
    )


def test_总结只跟着卡走_表填了也要重试():
    result, client = ask({**TABLE, "narrate": True}, TABLE)
    assert result.status == OK and "总结只跟着卡走" in client.calls[1][1]


def test_统计填了指标_重试():
    bad = {**STUDY, "metrics": [{"name": "市盈率", "expr": "$pe_ttm"}]}
    result, client = ask(bad, STUDY)
    assert result.status == OK and "事件统计不看展示指标" in client.calls[1][1]


def test_事件参数越界_问题里带上可选范围():
    bad = with_output(STUDY, event={"preset_id": "breakout_ma", "params": {"ma": 7}})
    good = with_output(STUDY, event={"preset_id": "breakout_ma", "params": {"ma": 250}})
    result, client = ask(bad, good)
    assert result.status == OK
    assert "5/10/20/60/120/250" in client.calls[1][1]


def test_行业名要照抄清单里的_概念板块不可用时不能用():
    result, client = ask({**TABLE, "scope": {"industry": "白酒"}}, TABLE)
    assert (
        result.status == OK and "scope.industry 要照抄现有条件里的申万行业名" in client.calls[1][1]
    )

    context = PlanContext(**{**CONTEXT.__dict__, "targets": ("stock", "sw_industry")})
    board = {**TABLE, "scope": {"target": "concept"}}
    result, client = ask(board, TABLE, context=context)
    assert result.status == OK and "通达信概念板块当前不可用" in client.calls[1][1]


def test_按板块排行时不能再限定板块():
    bad = {**TABLE, "scope": {"target": "sw_industry", "board": {"mention": "银行"}}}
    result, client = ask(bad, TABLE)
    assert result.status == OK and "不要再填 scope.board" in client.calls[1][1]


def test_没调用工具_原样再问一次():
    result, client = ask(LLMFormatError("大模型没有按格式返回（stop_reason=end_turn）"), TABLE)
    assert (result.status, result.attempts) == (OK, 2)
    assert client.calls[0][1] == client.calls[1][1] == "问题"  # 没有输出可改，原样再问
    assert result.calls[0].error and result.calls[1].error is None


def test_两次都没调用工具_返回failed():
    error = LLMFormatError("大模型没有按格式返回（stop_reason=max_tokens）")
    result, client = ask(error, error)
    assert result.status == FAILED and len(client.calls) == 2 and "max_tokens" in result.error


def test_大模型调用失败_返回failed():
    result, _ = ask(LLMError("大模型 120 秒没有回应"))
    assert (result.status, result.error) == (FAILED, "大模型 120 秒没有回应")


# ── 澄清、改写建议、追问 ────────────────────────────────────────


def test_澄清_选项最多三个_没有选项的问题丢掉():
    result, _ = ask(
        {
            "status": "needs_clarification",
            "questions": [
                {
                    "question": "「最近」指多久？",
                    "options": ["5 个交易日", "20 个交易日", "60 个交易日", "120 个交易日"],
                },
                {"question": "没有选项的", "options": []},
            ],
        }
    )
    assert result.status == CLARIFY
    assert result.questions == (
        Question("「最近」指多久？", ("5 个交易日", "20 个交易日", "60 个交易日")),
    )


def test_说要澄清却没给问题_重试():
    result, client = ask({"status": "needs_clarification"}, TABLE)
    assert result.status == OK and "questions 至少要有一个问题" in client.calls[1][1]


def test_回答不了_改写建议去掉买卖建议和百分比_最多三句():
    result, _ = ask(
        {
            "status": "unsupported",
            "message": "买卖建议回答不了",
            "alternatives": [
                "贵州茅台每次放量突破年线之后怎么走",
                "茅台现在能买入吗",
                "哪些股票明天会涨 20%",
                "今年以来涨幅最大的 50 只股票",
                "今年以来涨幅最大的 50 只股票",
                "最近 5 个交易日涨得最多的申万行业",
                "第五句",
            ],
        }
    )
    assert (result.status, result.message) == (UNSUPPORTED, "买卖建议回答不了")
    assert result.alternatives == (
        "贵州茅台每次放量突破年线之后怎么走",
        "今年以来涨幅最大的 50 只股票",
        "最近 5 个交易日涨得最多的申万行业",
    )


def test_改写建议太长的丢掉():
    assert clean_alternatives(["长" * 41, "短句"]) == ("短句",)


def test_过程记录_原始返回和_token_耗时都带出来_提示词只记哈希():
    """「大模型到底回了什么」以前只能靠猜，现在存下来（2026-09-16 加）。"""
    result, _ = ask(TABLE, query="昨天放量的股票")
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.attempt == 1 and call.prompt_id == "planner.system"
    assert call.raw_reply == TABLE  # 原始返回整份存
    assert call.user_message == "昨天放量的股票"
    assert call.problems == () and call.error is None
    assert call.seconds == 0.1
    # 提示词不整份存：模板哈希 + 渲染后哈希，两个都是 8 位
    assert len(call.prompt_version) == 8 and len(call.rendered_hash) == 8
    assert call.prompt_version != call.rendered_hash


def test_过程记录_重试时两次都留着_第一次的问题清单能看到():
    bad = with_output(TABLE, filter={"expr": "$不存在 > 1"})
    result, _ = ask(bad, TABLE)
    assert result.status == OK and result.attempts == 2
    assert len(result.calls) == 2
    first, second = result.calls
    assert first.raw_reply == bad and first.problems  # 第一次错在哪，留着
    assert any("不存在" in problem for problem in first.problems)
    assert second.attempt == 2 and second.problems == ()
    # 第二次发的是 planner.repair 渲染出来的，里面带着上一次的输出和问题
    assert "你上一次的输出" in second.user_message


def test_过程记录_调用本身失败时记下原因_没有原始返回():
    result, _ = ask(LLMError("大模型 180 秒没有回应"))
    assert result.status == FAILED
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.error == "大模型 180 秒没有回应" and call.raw_reply is None


def test_改条件_交给大模型的是现在的条件和要改的地方_不提原来的问题():
    """确认卡上用一句话改条件：不走追问那条路，那条路会让大模型从原话重新生成（2026-09-16 加）。"""
    now = {"subject": {"kind": "pool"}, "output": {"kind": "table", "limit": 20}}
    previous = PreviousTurn("最近哪个板块最强", ())
    _, client = ask(TABLE, query="改成前 5", previous=previous, revise=now)
    message = client.calls[0][1]
    assert '"limit": 20' in message and "现在的条件：" in message
    assert "用户要改的地方：\n改成前 5" in message
    assert "最近哪个板块最强" not in message


def test_改条件_看的对象没换时照抄代码_按代码查也不当成原话():
    draft = {**STUDY, "subject": {"kind": "codes", "codes": ["600519.SH"]}}
    result, _ = ask(draft, revise={"subject": {"kind": "codes", "codes": ["600519.SH"]}})
    assert result.status == OK
    assert result.subjects == (NameMention("600519.SH", is_code=True),)


def test_改条件_概念板块没换时照抄代码():
    draft = {**TABLE, "scope": {"board": {"code": "884001.TI"}}}
    result, _ = ask(draft, revise={"scope": {"board": {"type": "concept", "code": "884001.TI"}}})
    assert result.status == OK
    assert result.board == NameMention("884001.TI", is_code=True)


def test_追问_把原问题_问过的问题和回答一起交给大模型():
    previous = PreviousTurn(
        "最近哪个板块最强", (Question("「最近」指多久？", ("5 个交易日", "20 个交易日")),)
    )
    _, client = ask(TABLE, query="20 个交易日", previous=previous)
    message = client.calls[0][1]
    assert "最近哪个板块最强" in message
    assert "「最近」指多久？（选项：5 个交易日、20 个交易日）" in message
    assert "用户的回答：\n20 个交易日" in message
