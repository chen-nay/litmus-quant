"""第 7d 步：真实大模型 + 本地真实数据，按 ARCHITECTURE §9 第 7 步的验收标准逐条问一遍。

- 大模型的输出每次不完全一样：只核对验收要的结构（形状、股票代码、事件、状态、候选、默认值），不核对措辞
- 同一个问句只问一次（模块内缓存）。一次十几到几十秒，整个文件十来次调用、几分钟。平时不跑（Makefile 的 OFFLINE）
- 只调大模型和 /api/check，不计算结果、不同步。提问记录写临时目录
- 没配好大模型、本地没有数据就整个跳过

跑法：uv run pytest tests/test_plan_live.py -v -s   （-s 打出每个问句的耗时、状态、说明文字）
"""

from __future__ import annotations

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from litmus.api import Services, SyncJob, create_app
from litmus.data import DataService, DataSync, MarketStore, MissingDataError
from litmus.llm import AnthropicClient, LLMConfig, LLMError
from litmus.signals import load_events
from litmus.store import JsonStore

try:
    _config = LLMConfig.from_env()
except LLMError as exc:
    pytest.skip(f"大模型没配好：{exc}", allow_module_level=True)
_market = MarketStore.from_env()
_ds = DataService(_market)
try:
    _first, _last = _ds.data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的股票日频数据", allow_module_level=True)

HISTORY = "茅台每次放量突破年线之后表现怎么样？"


class Asker:
    def __init__(self, client: TestClient, store: JsonStore):
        self.client = client
        self.store = store
        self.answers: dict[tuple[str, str | None], dict] = {}

    def __call__(self, query: str, previous_plan_id: str | None = None) -> dict:
        key = (query, previous_plan_id)
        if key not in self.answers:
            started = time.perf_counter()
            body = self.client.post(
                "/api/plan", json={"query": query, "previous_plan_id": previous_plan_id}
            ).json()
            self._show(query, time.perf_counter() - started, body)
            self.answers[key] = body
        return self.answers[key]

    def _show(self, query: str, seconds: float, body: dict) -> None:
        record = self.store.get_plan(body["plan_id"]) if body.get("plan_id") else None
        attempts = record.detail.get("attempts") if record else None
        print(f"\n【{query}】{seconds:.0f} 秒，调用 {attempts} 次 → {body['status']}")
        if body.get("message"):
            print(f"    {body['message']}")
        for item in body["assumptions"]:
            print(f"    {'［默认］' if item['default'] else '      '}{item['text']}")
        for candidate in body["stock_candidates"] + body["board_candidates"]:
            print(f"    候选：{candidate['name']}（{candidate['code']}，{candidate['note']}）")
        for question in body["questions"]:
            print(f"    追问：{question['question']} {question['options']}")
        for alternative in body["alternatives"]:
            print(f"    改写建议：{alternative}")


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> TestClient:
    def no_sync():
        raise AssertionError("验收不同步")

    services = Services(
        ds=_ds,
        store=JsonStore(tmp_path_factory.mktemp("plans")),
        events=load_events(),
        sync_job=SyncJob(no_sync),
        data_status=DataSync(None, _market).status,
        llm=AnthropicClient(_config),
    )
    return TestClient(create_app(services))


@pytest.fixture(scope="module")
def ask(client) -> Asker:
    return Asker(client, client.app.state.services.store)


def items(body: dict) -> dict[str | None, dict]:
    return {item["field"]: item for item in body["assumptions"]}


def texts(body: dict) -> dict[str | None, str]:
    return {field: item["text"] for field, item in items(body).items()}


# ── 三种形状 ────────────────────────────────────────────────────


def test_股票表(ask):
    body = ask("最近一个交易日哪些股票成交额比前一周平均高 40% 以上？按放大倍数排前 20")
    assert body["status"] == "ok", body
    spec = body["spec"]
    assert (spec["shape"], spec["as_of"], spec["limit"]) == ("stock_list", _last.isoformat(), 20)
    assert "$amount" in spec["filter"]["expr"] and "1.4" in spec["filter"]["expr"]
    assert "$amount" in spec["sort"]["by"]


def test_板块表(ask):
    body = ask("最近 5 个交易日涨得最多的申万一级行业，前 5 名")
    assert body["status"] == "ok", body
    spec = body["spec"]
    assert (spec["shape"], spec["board_type"], spec["limit"]) == ("board_list", "sw_industry", 5)
    # Pct($close, 5) 或 Sum($pct_chg, 5)；2026-09-15 实测写成过 Pct($close, 4)
    assert spec["sort"]["order"] == "desc" and ",5)" in spec["sort"]["by"].replace(" ", "")


def test_今年以来_交易日数用代码算好的(ask):
    """2026-09-15 实测：不给日期换算表时，大模型自己数交易日，8000 个 token 用完也没给出结果。"""
    body = ask("今年以来涨幅最大的 50 只股票")
    assert body["status"] == "ok", body
    days = len(_ds.get_trading_calendar(date(date.today().year, 1, 1), _last))
    assert body["spec"]["sort"]["by"].replace(" ", "") == f"Pct($close,{days})"


def test_小市值_没给数字按默认30亿_标成默认值(ask):
    """2026-09-15 实测：没有默认值时大模型自己编门槛，名字写 30 亿、表达式写成了 300 亿。"""
    body = ask("小市值股票里昨天涨停的有哪些")
    assert body["status"] == "ok", body
    row = items(body)["filter"]
    assert "总市值 < 30 亿" in row["text"] and row["default"], row


def test_个股回看_没说的栏目用默认值并标出来(ask):
    body = ask(HISTORY)
    assert body["status"] == "ok", body
    spec = body["spec"]
    assert (spec["shape"], spec["target"]["code"]) == ("stock_history", "600519.SH")
    event = spec["event"]
    assert (event["preset_id"], event["params"]["ma"]) == ("breakout_ma_volume", 250)
    assert texts(body)["target"] == "「茅台」理解为：贵州茅台（600519.SH）"
    assert {"time_range", "horizons", "benchmark", "cost_bps"} <= set(spec["defaults_used"])


# ── 拦截、候选、追问、改写建议 ──────────────────────────────────


#: 字段清单里没有资金流向，大模型要么说回答不了，要么编了字段被防线②拦下
NO_SUCH_DATA = ["昨天北向资金净买入最多的 20 只股票", "昨天主力净流入超过 1 亿的股票"]


@pytest.mark.parametrize("query", NO_SUCH_DATA)
def test_数据里没有的_不会编个字段算出来(ask, query):
    body = ask(query)
    assert body["status"] in ("unsupported", "needs_clarification", "failed"), body
    assert not body["assumptions"]


def test_平安_对应多只股票_选一只之后出确认卡(ask, client):
    body = ask("平安每次放量之后一周涨跌怎样")
    assert body["status"] == "needs_clarification", body
    assert {"000001.SZ", "601318.SH"} <= {c["code"] for c in body["stock_candidates"]}

    # 页面上点候选：只填代码（web/src/planFlow.ts 的 withStock）
    spec = {**body["spec"], "target": {**body["spec"].get("target", {}), "code": "601318.SH"}}
    checked = client.post("/api/check", json={"spec": spec, "plan_id": body["plan_id"]}).json()
    assert checked["status"] == "ok", checked
    assert checked["spec"]["event"]["preset_id"] == "volume_surge"
    assert checked["spec"]["horizons"] == [5]
    assert texts(checked)["target"] == "「平安」理解为：中国平安（601318.SH）"


def test_最近哪个板块最强_先追问_回答之后出板块表(ask):
    first = ask("最近哪个板块最强")
    assert first["status"] == "needs_clarification", first
    assert first["questions"] and not first["stock_candidates"]
    assert all(2 <= len(question["options"]) <= 3 for question in first["questions"])

    # 每个问题选第一个选项，拼法同 web/src/planFlow.ts 的 composeAnswer
    answer = "；".join(f"{q['question']}{q['options'][0]}" for q in first["questions"])
    second = ask(answer, first["plan_id"])
    assert second["status"] == "ok", second
    assert second["spec"]["shape"] == "board_list"


def test_现在能买茅台吗_给改写建议_建议本身能回答(ask):
    body = ask("现在能买茅台吗")
    assert body["status"] == "unsupported", body
    assert body["alternatives"]
    follow = ask(body["alternatives"][0])
    assert follow["status"] in ("ok", "needs_clarification"), follow


def test_条件不是事件_说明原因并给改写建议(ask):
    body = ask("茅台市值低于 2000 亿之后的表现")
    assert body["status"] == "not_an_event", body
    assert body["message"] and body["alternatives"]


# ── 确认卡上改参数 ──────────────────────────────────────────────


def test_确认卡上改了参数_说明文字跟着变_没改的照旧(ask, client):
    body = ask(HISTORY)
    assert body["status"] == "ok", body
    spec = body["spec"]
    event = {**spec["event"], "params": {**spec["event"]["params"], "ma": 60}}
    edited = {**spec, "event": event, "cost_bps": 50}
    checked = client.post("/api/check", json={"spec": edited, "plan_id": body["plan_id"]}).json()
    assert checked["status"] == "ok", checked

    before, after = texts(body), texts(checked)
    assert "250 日" in before["event"] and "60 日" in after["event"]
    assert "放量突破年线" not in after["event"]  # 改过的栏目不再说「「放量突破年线」理解为」
    assert after["target"] == before["target"]
    assert "0.50%" in after["cost_bps"] and not items(checked)["cost_bps"]["default"]
