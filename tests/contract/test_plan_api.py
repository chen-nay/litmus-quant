"""POST /api/plan 在本地真实数据上的契约测试：大模型用替身，核对防线③（股票、概念板块解析）、说明文字、追问。

不调真实大模型（真实问题集在第 7d 步）。数据量都很小。没有本地数据就整个跳过。
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from litmus.api import Services, SyncJob, create_app
from litmus.data import (
    CONCEPT,
    SW_INDUSTRY_L2,
    DataService,
    DataSync,
    MarketStore,
    MissingDataError,
)
from litmus.llm import LLMClient, StructuredReply
from litmus.signals import load_events
from litmus.store import JsonStore

DAY = date(2026, 9, 11)
_market = MarketStore.from_env()
_ds = DataService(_market)
try:
    _first, _last = _ds.data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的股票日频数据", allow_module_level=True)
if _last < DAY:
    pytest.skip(f"用例要 {DAY} 的数据，本地只到 {_last}", allow_module_level=True)


class FakeLLM(LLMClient):
    def __init__(self):
        self.outputs: list[dict] = []
        self.calls: list[str] = []

    def structured(self, system, user, schema):
        self.calls.append(user)
        return StructuredReply(self.outputs.pop(0), 0.1)


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def client(tmp_path, llm) -> TestClient:
    def no_sync():
        raise AssertionError("契约测试不同步")

    services = Services(
        ds=_ds,
        store=JsonStore(tmp_path),
        events=load_events(),
        sync_job=SyncJob(no_sync),
        data_status=DataSync(None, _market).status,
        llm=llm,
    )
    return TestClient(create_app(services))


def ask(client: TestClient, llm: FakeLLM, output: dict, query: str = "问题", **extra) -> dict:
    llm.outputs.append(output)
    return client.post("/api/plan", json={"query": query, **extra}).json()


TABLE = {
    "status": "ok",
    "subject": {"kind": "pool"},
    "when": {"as_of": "2026-09-11"},
    "metrics": [{"name": "放大倍数", "expr": "$amount / Mean(Ref($amount, 1), 5)"}],
    "output": {
        "kind": "table",
        "filter": {"expr": "$amount > Mean(Ref($amount, 1), 5) * 1.4", "label": "放量"},
        "sort": {"by": "放大倍数"},
        "limit": 20,
    },
    "mentions": [
        {"phrase": "昨天", "field": "when.as_of"},
        {"phrase": "成交量明显放大", "field": "output.filter"},
    ],
}

HISTORY = {
    "status": "ok",
    "subject": {"kind": "codes", "mentions": [{"mention": "茅台", "guess": "贵州茅台"}]},
    "output": {
        "kind": "event_study",
        "event": {"preset_id": "breakout_ma_volume", "params": {"ma": 250}},
    },
    "mentions": [
        {"phrase": "茅台", "field": "subject"},
        {"phrase": "放量突破年线", "field": "output.event"},
    ],
}

CARD = {
    "status": "ok",
    "scope": {"board": {"mention": "农林牧渔", "guess": "农林牧渔"}},
    "subject": {"kind": "codes", "mentions": [{"mention": "牧原", "guess": "牧原股份"}]},
    "when": {"as_of": DAY.isoformat()},
    "metrics": [{"name": "今年以来涨幅排名", "expr": "Rank(PctSince($close, 20251231))"}],
    "output": {"kind": "card"},
}


def in_board(draft: dict, mention: str | None = None, guess: str | None = None, **board) -> dict:
    """限定在某个行业、板块里：原话 + 猜的名字，或者照抄的代码。"""
    named = {"mention": mention, "guess": guess} if mention else {}
    return {**draft, "scope": {"board": {**named, **board}}}


def naming(draft: dict, *names: tuple[str, str]) -> dict:
    """点名看的标的换成这几个（原话, 猜的全称）。"""
    mentions = [{"mention": mention, "guess": guess} for mention, guess in names]
    return {**draft, "subject": {"kind": "codes", "mentions": mentions}}


def picks(body: dict, slot: str | None = None) -> list[dict]:
    """要用户选的候选，几组合在一起；给了 slot 只看那一栏的。"""
    return [
        c
        for choice in body["choices"]
        if slot in (None, choice["slot"])
        for c in choice["candidates"]
    ]


def texts(body: dict) -> dict[str | None, str]:
    return {item["field"]: item["text"] for item in body["assumptions"]}


def test_股票表_说明文字带上原话的说法(client, llm):
    body = ask(client, llm, TABLE, query="昨天哪个股票成交量明显放大")
    assert body["status"] == "ok", body
    assert body["plan_id"].startswith("p")
    assert texts(body)["when.as_of"] == "「昨天」理解为：2026-09-11"
    assert texts(body)["output.filter"] == (
        "「成交量明显放大」理解为：成交额 > 前 5 日成交额均值（不含当天） × 1.4"
    )


def test_确认卡上改了参数_改过的栏目不再用原话的说法(client, llm):
    body = ask(client, llm, TABLE)
    spec = {**body["spec"], "when": {"as_of": "2026-09-10"}}
    checked = client.post("/api/check", json={"spec": spec, "plan_id": body["plan_id"]}).json()
    assert checked["status"] == "ok", checked
    assert texts(checked)["when.as_of"] == "日期：2026-09-10"
    assert texts(checked)["output.filter"].startswith("「成交量明显放大」理解为")


def test_个股回看_股票按原话解析成代码_没说的区间用本地全部数据(client, llm):
    body = ask(client, llm, HISTORY)
    assert body["status"] == "ok", body
    assert body["spec"]["subject"]["codes"][0] == "600519.SH"
    items = {item["field"]: item for item in body["assumptions"]}
    assert items["subject"]["text"] == "「茅台」理解为：贵州茅台（600519.SH）"
    assert items["when.range"]["default"] is True


def test_大模型没记原话的说法_股票和概念板块的原话照样用上(client, llm):
    without = {key: value for key, value in HISTORY.items() if key != "mentions"}
    body = ask(client, llm, without)
    assert body["status"] == "ok", body
    assert texts(body)["subject"] == "「茅台」理解为：贵州茅台（600519.SH）"

    if CONCEPT not in _ds.available_targets():
        return
    body = ask(client, llm, in_board(TABLE, "光模块", "光通信"))
    assert body["status"] == "ok", body
    assert texts(body)["scope.board"].startswith("「光模块」理解为：光通信")


def test_卡不走确认卡_提问这一步就算完(client, llm):
    body = ask(client, llm, CARD, query="牧原在农林牧渔里今年涨幅排第几")

    assert body["status"] == "done", body
    assert body["result"]["kind"] == "card"
    assert body["result"]["items"][0]["name"] == "牧原股份"
    assert body["run_id"].startswith("r") and body["plan_id"].startswith("p")
    assert not body["assumptions"]  # 说明跟着结果走，在卡底下
    footer = {item["field"]: item for item in body["result"]["assumptions"]}
    assert footer["scope.base"]["default"] and footer["scope.exclude"]["default"]
    # 原话就是行业名，不写「「农林牧渔」理解为：农林牧渔」
    assert footer["scope.industry"]["text"].startswith("算的范围：农林牧渔（申万一级行业")

    run = client.get(f"/api/run/{body['run_id']}").json()
    assert run["plan_id"] == body["plan_id"] and run["status"] == "done"
    steps = [step["step"] for step in client.get(f"/api/traces/{body['plan_id']}").json()["steps"]]
    assert steps[-2:] == ["run", "respond"]


def test_卡先出_小结再单独取_写了数字重试一次_写过就不重写(client, llm):
    llm.outputs.append({**CARD, "narrate": True})
    query = "牧原在农林牧渔里今年涨幅排第几，怎么样"
    body = client.post("/api/plan", json={"query": query}).json()
    assert body["status"] == "done", body
    assert body["result"]["narrate"] is True  # 页面据此先占着小结的位置
    assert len(llm.calls) == 1  # 卡先出，没等小结

    llm.outputs += [
        {"text": "今年以来跌了 15.69%，比行业差。"},
        {"text": "今年以来在跌，而且比农林牧渔行业跌得多。"},
    ]
    url = f"/api/run/{body['run_id']}/narrative"
    first = client.post(url).json()
    assert first == {"text": "今年以来在跌，而且比农林牧渔行业跌得多。", "error": None}
    # 交给大模型的是用户原话和卡上的文字
    assert llm.calls[1].startswith(f"问题：{query}\n卡：\n牧原股份")

    assert client.post(url).json() == first and len(llm.calls) == 3  # 写过就给存下的那段
    assert client.get(f"/api/run/{body['run_id']}").json()["narrative"] == first["text"]


def test_卡不要小结时_取小结报错(client, llm):
    body = ask(client, llm, CARD)
    assert body["status"] == "done" and body["result"]["narrate"] is False
    response = client.post(f"/api/run/{body['run_id']}/narrative")
    assert response.status_code == 400 and "不是要写小结的卡" in response.json()["detail"]
    assert client.get(f"/api/run/{body['run_id']}").json()["narrative"] is None


def test_卡_点名两只股票_各出一份(client, llm):
    draft = naming(CARD, ("牧原", "牧原股份"), ("温氏", "温氏股份"))
    body = ask(client, llm, {**draft, "scope": {}}, query="牧原跟温氏今年谁涨得多")
    assert body["status"] == "done", body
    assert [item["name"] for item in body["result"]["items"]] == ["牧原股份", "温氏股份"]


def test_点名的板块只包含对上_不直接用_让用户选(client, llm):
    """2026-09-18 实测：「新能源」只包含在「新能源车」里就直接用了，看的其实是另一个板块。"""
    draft = {**naming(CARD, ("银", "银")), "scope": {"target": "sw_industry"}}
    body = ask(client, llm, draft)
    assert body["status"] == "needs_clarification", body
    assert body["message"].startswith("没有叫「银」的申万一级行业，名字相近的是下面这些")
    assert "银行" in {c["name"] for c in picks(body)}


def test_两种说法指的是同一只_只留一个代码(client, llm):
    """2026-09-18 实测：「茅台每次放量突破年线之后」大模型把茅台点了两次，统计因此报「只支持点名一只」。"""
    body = ask(client, llm, naming(HISTORY, ("茅台", "贵州茅台"), ("贵州茅台", "贵州茅台")))
    assert body["status"] == "ok", body
    assert body["spec"]["subject"]["codes"] == ["600519.SH"]


def test_两只都对应多只_一次都给出来_各选各的(client, llm):
    """「平安和茅台」：平安对应 3 只；再点一个查不准的「中国」，两组候选一次给，填进「看谁」。"""
    draft = naming(CARD, ("平安", "中国平安"), ("中国", "中国"))
    body = ask(client, llm, {**draft, "scope": {}})
    assert body["status"] == "needs_clarification", body
    assert [(c["slot"], c["mention"]) for c in body["choices"]] == [
        ("subject", "平安"),
        ("subject", "中国"),
    ]
    assert body["message"] == "有几个说法对应不止一个，各选一个"


def test_限定的板块对应不止一个_候选填进算的范围(client, llm):
    if CONCEPT not in _ds.available_targets():
        pytest.skip("概念板块不可用")
    body = ask(client, llm, in_board(TABLE, "光模块", "光通信、CPO概念"))
    assert [(c["slot"], c["mention"]) for c in body["choices"]] == [("scope", "光模块")]


def test_卡_其中一只对应多只股票_查准的留着_让用户选另一只(client, llm):
    draft = naming(CARD, ("牧原", "牧原股份"), ("平安", "中国平安"))
    body = ask(client, llm, {**draft, "scope": {}})
    assert body["status"] == "needs_clarification", body
    assert body["message"].startswith("「平安」对应")
    assert body["spec"]["subject"]["codes"] == ["002714.SZ"]


def test_卡_点名的是板块_按那一类板块查(client, llm):
    draft = {
        **naming(CARD, ("银行", "银行")),
        "scope": {"target": "sw_industry"},
        "metrics": [{"name": "近 20 日涨幅排名", "expr": "Rank(Pct($close, 20))"}],
    }
    body = ask(client, llm, draft, query="银行最近 20 天在申万行业里涨幅排第几")
    assert body["status"] == "done", body
    item = body["result"]["items"][0]
    assert item["name"] == "银行" and "个）" in item["rows"][0]["text"]


def test_个股回看_平安对应多只股票_让用户选(client, llm):
    body = ask(client, llm, naming(HISTORY, ("平安", "中国平安")))
    assert body["status"] == "needs_clarification", body
    assert {"000001.SZ", "601318.SH", "001359.SZ"} <= {c["code"] for c in picks(body, "subject")}
    assert "codes" not in body["spec"]["subject"]  # 还没选


def test_没找到的股票_让用户换个说法(client, llm):
    body = ask(client, llm, naming(HISTORY, ("不存在的公司", "也不存在")))
    assert body["status"] == "needs_clarification"
    assert "没找到「不存在的公司」" in body["message"]


def test_选股限定概念板块_原话查不到用猜的名字(client, llm):
    if CONCEPT not in _ds.available_targets():
        pytest.skip("概念板块不可用")
    body = ask(client, llm, in_board(TABLE, "光模块", "光通信"))
    assert body["status"] == "ok", body
    board = next(b for b in _ds.list_boards(CONCEPT) if b.name == "光通信")
    assert body["spec"]["scope"]["board"]["code"] == board.code


def test_半导体板块_能对上申万二级就用_只包含对上第三代半导体时让用户选(client, llm):
    """2026-09-15 实测：「半导体板块」只包含对上「第三代半导体」，直接用了它，范围窄得离谱。"""
    body = ask(client, llm, in_board(TABLE, "半导体板块", "芯片"))
    if SW_INDUSTRY_L2 in _ds.available_targets():
        assert body["status"] == "ok", body
        assert body["spec"]["scope"]["industry"] == "半导体"
        assert texts(body)["scope.industry"].startswith(
            "「半导体板块」理解为：半导体（申万二级行业"
        )
    else:  # 本地还没同步二级
        assert body["status"] == "needs_clarification", body
        assert {"第三代半导体", "芯片"} <= {c["name"] for c in picks(body)}


def test_概念板块猜了几个名字_都对得上就让用户选(client, llm):
    if CONCEPT not in _ds.available_targets():
        pytest.skip("概念板块不可用")
    output = in_board(TABLE, "光模块", "光通信、CPO概念")
    body = ask(client, llm, output)
    assert body["status"] == "needs_clarification", body
    assert {"光通信", "CPO概念"} <= {c["name"] for c in picks(body)}


def test_概念板块原话和猜测名都查不到_列出名字相近的让用户选(client, llm):
    if CONCEPT not in _ds.available_targets():
        pytest.skip("概念板块不可用")
    output = in_board(TABLE, "机器人灵巧手", "没有这个板块")
    body = ask(client, llm, output)
    assert body["status"] == "needs_clarification", body
    assert body["message"].startswith("没找到叫「机器人灵巧手」的行业或板块")
    assert "机器人概念" in {c["name"] for c in picks(body)}

    # 连名字相近的都没有：不给接口地址，让用户换个说法或者去表单里选
    output = in_board(TABLE, "光模块", "没有这个板块")
    body = ask(client, llm, output)
    assert (body["status"], body["choices"]) == ("needs_clarification", [])
    assert "换个说法" in body["message"] and "/api/" not in body["message"]


def test_过程记录_原始返回_token_耗时_解析步骤都记下来(client, llm):
    """2026-09-16 加：以前「大模型到底回了什么」只能靠猜，现在按 plan_id 就能翻出来。"""
    body = ask(client, llm, HISTORY, query="茅台放量突破年线之后怎么样")
    assert body["status"] == "ok", body

    trace = client.get(f"/api/traces/{body['plan_id']}").json()
    assert trace["record_id"] == body["plan_id"]
    assert trace["query"] == "茅台放量突破年线之后怎么样" and trace["created_at"]

    steps = {step["step"]: step for step in trace["steps"]}
    assert set(steps) == {"llm.plan", "resolve_stock", "check_spec", "respond"}

    call = steps["llm.plan"]
    assert call["raw_reply"] == HISTORY  # 原始返回整份留着
    assert call["prompt_id"] == "planner.system"
    assert len(call["prompt_version"]) == 8 and len(call["rendered_hash"]) == 8
    assert call["problems"] == [] and call["error"] is None

    assert steps["resolve_stock"]["mention"] == "茅台"
    assert steps["resolve_stock"]["matches"][0]["code"] == "600519.SH"
    assert steps["check_spec"]["issues"] == []
    assert steps["respond"]["status"] == "ok"


def test_过程记录_被拒绝的提问也记_看得出是大模型自己回的unsupported(client, llm):
    output = {
        "status": "unsupported",
        "message": "单只股票当前的市盈率不在系统支持的三类查询里",
        "alternatives": ["茅台放量突破年线之后表现怎么样"],
    }
    body = ask(client, llm, output, query="牧原股份现在市盈率多少？")
    assert body["status"] == "unsupported"

    trace = client.get(f"/api/traces/{body['plan_id']}").json()
    call = next(s for s in trace["steps"] if s["step"] == "llm.plan")
    assert call["raw_reply"] == output  # 这句「回答不了」是大模型自己写的，白纸黑字
    # 拒绝时不走防线③，只有调用和最后的回应
    assert [s["step"] for s in trace["steps"]] == ["llm.plan", "respond"]


def test_过程记录存不下来_不影响回答(client, llm, monkeypatch):
    """过程记录是给开发看的，用户的答案已经算好了，不能因为记不上就整个失败。"""
    monkeypatch.setattr(
        JsonStore, "save_trace", lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘满了"))
    )
    body = ask(client, llm, HISTORY, query="茅台放量突破年线之后怎么样")
    assert body["status"] == "ok", body
    assert client.get(f"/api/traces/{body['plan_id']}").status_code == 404


def test_确认卡上改条件_把现在的条件交给大模型_选过的概念板块不会丢(client, llm):
    """2026-09-16 加：改条件走追问那条路会从原话重新生成，「光模块」又变回一堆候选让人重选。"""
    if CONCEPT not in _ds.available_targets():
        pytest.skip("概念板块不可用")
    board = next(b for b in _ds.list_boards(CONCEPT) if b.name == "光通信")
    first = ask(
        client,
        llm,
        in_board(TABLE, "光模块", "光通信"),
        query="光模块里最近放量的股票",
    )
    assert first["status"] == "ok", first

    # 大模型照抄板块代码、只改取前几名
    second = ask(
        client,
        llm,
        in_board({**TABLE, "output": {**TABLE["output"], "limit": 5}}, code=board.code),
        query="改成前 5",
        previous_plan_id=first["plan_id"],
        spec=first["spec"],
    )
    assert second["status"] == "ok", second
    assert second["spec"]["scope"]["board"]["code"] == board.code
    assert second["spec"]["output"]["limit"] == 5
    # 照抄的代码不是用户原话，确认卡上不写「「884xxx.TI」理解为：光通信」
    assert board.code not in "".join(item["text"] for item in second["assumptions"])

    message = llm.calls[-1]
    assert "现在的条件：" in message and board.code in message
    assert "用户要改的地方：\n改成前 5" in message
    assert "光模块里最近放量的股票" not in message


def test_确认卡上改条件_股票照抄代码_确认卡上不写成原话(client, llm):
    first = ask(client, llm, HISTORY, query="茅台放量突破年线之后怎样")
    assert first["status"] == "ok", first
    assert texts(first)["subject"].startswith("「茅台」理解为")

    code = first["spec"]["subject"]["codes"][0]
    output = {**HISTORY["output"], "horizons": [5, 10]}
    second = ask(
        client,
        llm,
        {
            **HISTORY,
            "subject": {"kind": "codes", "codes": [code]},
            "output": output,
            "mentions": [],
        },
        query="看 5 天和 10 天",
        previous_plan_id=first["plan_id"],
        spec=first["spec"],
    )
    assert second["status"] == "ok", second
    assert second["spec"]["subject"]["codes"][0] == code
    assert second["spec"]["output"]["horizons"] == [5, 10]
    # 照抄的代码不是用户原话：确认卡上写「股票：贵州茅台（600519.SH）」，不写「「600519.SH」理解为……」
    assert "理解为" not in texts(second)["subject"]
    # 交给大模型的条件里股票只留代码：2026-09-16 实测留着 mention 它就照抄，于是又按名字重查一遍
    assert code in llm.calls[-1] and "茅台" not in llm.calls[-1]


def test_澄清之后追问_把原问题和回答一起交给大模型(client, llm):
    question = {"question": "「最近」指多久？", "options": ["5 个交易日", "20 个交易日"]}
    first = ask(
        client,
        llm,
        {"status": "needs_clarification", "questions": [question]},
        query="最近哪个板块最强",
    )
    assert first["status"] == "needs_clarification"
    assert first["questions"] == [question]
    output = {
        "status": "ok",
        "scope": {"target": "sw_industry"},
        "subject": {"kind": "pool"},
        "metrics": [{"name": "20 日涨幅", "expr": "Pct($close, 20)"}],
        "output": {"kind": "table", "sort": {"by": "20 日涨幅"}},
    }
    second = ask(client, llm, output, query="20 个交易日", previous_plan_id=first["plan_id"])
    assert second["status"] == "ok", second
    assert "最近哪个板块最强" in llm.calls[-1] and "「最近」指多久？" in llm.calls[-1]
    assert second["spec"]["defaults_used"][:2] == ["when", "scope.base"]


def test_大模型给的日期不是交易日_转成澄清(client, llm):
    body = ask(client, llm, {**TABLE, "when": {"as_of": "2026-09-06"}})
    assert body["status"] == "needs_clarification"
    assert "不是交易日" in body["message"]


def test_回答不了_给改写建议(client, llm):
    output = {
        "status": "unsupported",
        "message": "买卖建议回答不了",
        "alternatives": ["茅台每次放量突破年线之后怎么走", "现在能买入吗"],
    }
    body = ask(client, llm, output)
    assert (body["status"], body["alternatives"]) == (
        "unsupported",
        ["茅台每次放量突破年线之后怎么走"],
    )
