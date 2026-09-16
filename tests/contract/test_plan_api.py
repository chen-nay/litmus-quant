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


STOCK_LIST = {
    "status": "ok",
    "shape": "stock_list",
    "as_of": "2026-09-11",
    "filter_expr": "$amount > Mean(Ref($amount, 1), 5) * 1.4",
    "filter_label": "放量",
    "sort_by": "$amount / Mean(Ref($amount, 1), 5)",
    "sort_label": "放大倍数",
    "limit": 20,
    "mentions": [
        {"phrase": "昨天", "field": "as_of"},
        {"phrase": "成交量明显放大", "field": "filter"},
    ],
}

HISTORY = {
    "status": "ok",
    "shape": "stock_history",
    "stock_mention": "茅台",
    "stock_guess": "贵州茅台",
    "event_id": "breakout_ma_volume",
    "event_params": {"ma": 250},
    "mentions": [
        {"phrase": "茅台", "field": "target"},
        {"phrase": "放量突破年线", "field": "event"},
    ],
}


def texts(body: dict) -> dict[str | None, str]:
    return {item["field"]: item["text"] for item in body["assumptions"]}


def test_股票表_说明文字带上原话的说法(client, llm):
    body = ask(client, llm, STOCK_LIST, query="昨天哪个股票成交量明显放大")
    assert body["status"] == "ok", body
    assert body["plan_id"].startswith("p")
    assert texts(body)["as_of"] == "「昨天」理解为：2026-09-11"
    assert texts(body)["filter"] == (
        "「成交量明显放大」理解为：成交额 > 前 5 日成交额均值（不含当天） × 1.4"
    )


def test_确认卡上改了参数_改过的栏目不再用原话的说法(client, llm):
    body = ask(client, llm, STOCK_LIST)
    spec = {**body["spec"], "as_of": "2026-09-10"}
    checked = client.post("/api/check", json={"spec": spec, "plan_id": body["plan_id"]}).json()
    assert checked["status"] == "ok", checked
    assert texts(checked)["as_of"] == "日期：2026-09-10"
    assert texts(checked)["filter"].startswith("「成交量明显放大」理解为")


def test_个股回看_股票按原话解析成代码_没说的区间用本地全部数据(client, llm):
    body = ask(client, llm, HISTORY)
    assert body["status"] == "ok", body
    assert body["spec"]["target"]["code"] == "600519.SH"
    items = {item["field"]: item for item in body["assumptions"]}
    assert items["target"]["text"] == "「茅台」理解为：贵州茅台（600519.SH）"
    assert items["time_range"]["default"] is True


def test_大模型没记原话的说法_股票和概念板块的原话照样用上(client, llm):
    without = {key: value for key, value in HISTORY.items() if key != "mentions"}
    body = ask(client, llm, without)
    assert body["status"] == "ok", body
    assert texts(body)["target"] == "「茅台」理解为：贵州茅台（600519.SH）"

    if CONCEPT not in _ds.available_targets():
        return
    body = ask(client, llm, {**STOCK_LIST, "board_mention": "光模块", "board_guess": "光通信"})
    assert body["status"] == "ok", body
    assert texts(body)["universe.board"] == "「光模块」理解为：光通信"


def test_个股回看_平安对应多只股票_让用户选(client, llm):
    body = ask(client, llm, {**HISTORY, "stock_mention": "平安", "stock_guess": "中国平安"})
    assert body["status"] == "needs_clarification", body
    assert {"000001.SZ", "601318.SH", "001359.SZ"} <= {c["code"] for c in body["stock_candidates"]}
    assert "target" not in body["spec"]


def test_没找到的股票_让用户换个说法(client, llm):
    body = ask(client, llm, {**HISTORY, "stock_mention": "不存在的公司", "stock_guess": "也不存在"})
    assert body["status"] == "needs_clarification"
    assert "没找到「不存在的公司」" in body["message"]


def test_选股限定概念板块_原话查不到用猜的名字(client, llm):
    if CONCEPT not in _ds.available_targets():
        pytest.skip("概念板块不可用")
    body = ask(client, llm, {**STOCK_LIST, "board_mention": "光模块", "board_guess": "光通信"})
    assert body["status"] == "ok", body
    board = next(b for b in _ds.list_boards(CONCEPT) if b.name == "光通信")
    assert body["spec"]["universe"]["board"]["code"] == board.code


def test_半导体板块_能对上申万二级就用_只包含对上第三代半导体时让用户选(client, llm):
    """2026-09-15 实测：「半导体板块」只包含对上「第三代半导体」，直接用了它，范围窄得离谱。"""
    body = ask(client, llm, {**STOCK_LIST, "board_mention": "半导体板块", "board_guess": "芯片"})
    if SW_INDUSTRY_L2 in _ds.available_targets():
        assert body["status"] == "ok", body
        assert body["spec"]["universe"]["industry"] == "半导体"
        assert texts(body)["universe.industry"].startswith(
            "「半导体板块」理解为：半导体（申万二级行业"
        )
    else:  # 本地还没同步二级
        assert body["status"] == "needs_clarification", body
        assert {"第三代半导体", "芯片"} <= {c["name"] for c in body["board_candidates"]}


def test_概念板块猜了几个名字_都对得上就让用户选(client, llm):
    if CONCEPT not in _ds.available_targets():
        pytest.skip("概念板块不可用")
    output = {**STOCK_LIST, "board_mention": "光模块", "board_guess": "光通信、CPO概念"}
    body = ask(client, llm, output)
    assert body["status"] == "needs_clarification", body
    assert {"光通信", "CPO概念"} <= {c["name"] for c in body["board_candidates"]}


def test_概念板块原话和猜测名都查不到_列出名字相近的让用户选(client, llm):
    if CONCEPT not in _ds.available_targets():
        pytest.skip("概念板块不可用")
    output = {**STOCK_LIST, "board_mention": "机器人灵巧手", "board_guess": "没有这个板块"}
    body = ask(client, llm, output)
    assert body["status"] == "needs_clarification", body
    assert body["message"].startswith("没找到叫「机器人灵巧手」的行业或板块")
    assert "机器人概念" in {c["name"] for c in body["board_candidates"]}

    # 连名字相近的都没有：不给接口地址，让用户换个说法或者去表单里选
    output = {**STOCK_LIST, "board_mention": "光模块", "board_guess": "没有这个板块"}
    body = ask(client, llm, output)
    assert (body["status"], body["board_candidates"]) == ("needs_clarification", [])
    assert "打开表单" in body["message"] and "/api/" not in body["message"]


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
        {**STOCK_LIST, "board_mention": "光模块", "board_guess": "光通信"},
        query="光模块里最近放量的股票",
    )
    assert first["status"] == "ok", first

    # 大模型照抄板块代码、只改取前几名
    second = ask(
        client,
        llm,
        {**STOCK_LIST, "limit": 5, "board_code": board.code},
        query="改成前 5",
        previous_plan_id=first["plan_id"],
        spec=first["spec"],
    )
    assert second["status"] == "ok", second
    assert second["spec"]["universe"]["board"]["code"] == board.code
    assert second["spec"]["limit"] == 5
    # 照抄的代码不是用户原话，确认卡上不写「「884xxx.TI」理解为：光通信」
    assert board.code not in "".join(item["text"] for item in second["assumptions"])

    message = llm.calls[-1]
    assert "现在的条件：" in message and board.code in message
    assert "用户要改的地方：\n改成前 5" in message
    assert "光模块里最近放量的股票" not in message


def test_确认卡上改条件_股票照抄代码_确认卡上不写成原话(client, llm):
    first = ask(client, llm, HISTORY, query="茅台放量突破年线之后怎样")
    assert first["status"] == "ok", first
    assert texts(first)["target"].startswith("「茅台」理解为")

    code = first["spec"]["target"]["code"]
    output = {key: value for key, value in HISTORY.items() if not key.startswith("stock_")}
    second = ask(
        client,
        llm,
        {**output, "stock_code": code, "horizons": [5, 10], "mentions": []},
        query="看 5 天和 10 天",
        previous_plan_id=first["plan_id"],
        spec=first["spec"],
    )
    assert second["status"] == "ok", second
    assert second["spec"]["target"]["code"] == code
    assert second["spec"]["horizons"] == [5, 10]
    # 照抄的代码不是用户原话：确认卡上写「股票：贵州茅台（600519.SH）」，不写「「600519.SH」理解为……」
    assert "理解为" not in texts(second)["target"]
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
        "shape": "board_list",
        "board_type": "sw_industry",
        "sort_by": "Pct($close, 20)",
        "sort_label": "20 日涨幅",
    }
    second = ask(client, llm, output, query="20 个交易日", previous_plan_id=first["plan_id"])
    assert second["status"] == "ok", second
    assert "最近哪个板块最强" in llm.calls[-1] and "「最近」指多久？" in llm.calls[-1]
    assert second["spec"]["defaults_used"][:2] == ["as_of", "limit"]


def test_大模型给的日期不是交易日_转成澄清(client, llm):
    body = ask(client, llm, {**STOCK_LIST, "as_of": "2026-09-06"})
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
