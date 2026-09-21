"""HTTP 接口的离线测试：TestClient + 替身，不读本地数据、不连 Tushare。

真实数据上跑三种结果的见 tests/contract/test_run_api.py。
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi.testclient import TestClient

from litmus.api import Services, SyncJob, create_app
from litmus.data import (
    STOCK,
    SW_INDUSTRY,
    BoardInfo,
    DataDate,
    DataStatus,
    MissingDataError,
    MonthResult,
)
from litmus.llm import LLMClient
from litmus.research import Column, ListResult
from litmus.signals import load_events
from litmus.store import JsonStore

EVENTS = load_events()
DAY = date(2026, 9, 11)  # 星期五
READY = DataStatus(
    ready=True,
    reason="",
    data_through="2026-09-11",
    history_from="2016-01-04",
    history_done=True,
    unlock_months=(),
    unlock_missing=(),
    synced_at={},
    unavailable={},
)


class FakeData:
    """只实现检查要用的方法：股票数据 2016-01-04 ~ 2026-09-11，周末不开市，概念板块不可用。"""

    def available_targets(self):
        return (STOCK, SW_INDUSTRY)

    def data_range(self, target=STOCK):
        return date(2016, 1, 4), DAY

    def get_trading_calendar(self, start, end):
        return [start] if start == end and start.weekday() < 5 else []

    def latest_dates(self):
        return [DataDate("stock", "股票行情", DAY), DataDate("sw_industry", "申万行业行情", DAY)]

    def list_boards(self, board_type):
        if board_type != SW_INDUSTRY:
            raise MissingDataError("concept 当前不可用：没有权限")
        return [
            BoardInfo("801010.SI", "农林牧渔", SW_INDUSTRY),
            BoardInfo("801780.SI", "银行", SW_INDUSTRY),
        ]

    def get_universe_mask(self, start, end, **_):
        return pl.DataFrame({"date": [DAY] * 2, "code": ["600519.SH", "000001.SZ"]})

    def stock_info(self, codes, day):
        names = {"600519.SH": "贵州茅台", "000001.SZ": "平安银行"}
        return pl.DataFrame(
            {
                "code": list(codes),
                "name": [names.get(c, "") for c in codes],
                "industry": ["食品饮料"] * len(codes),
                "list_date": [date(2001, 8, 27) if c in names else None for c in codes],
                "delist_date": [None] * len(codes),
            }
        )


class NoData:
    """结构、事件检查就该拦下的请求用它：一碰数据就说明检查顺序错了。"""

    def __getattr__(self, name):
        raise AssertionError(f"这个请求不该读数据（{name}）")


def no_sync():
    raise AssertionError("这个测试不该同步")


def make_client(tmp_path, ds=None, status=READY, job=None, llm=None) -> TestClient:
    services = Services(
        ds=ds or FakeData(),
        store=JsonStore(tmp_path),
        events=EVENTS,
        sync_job=job or SyncJob(no_sync),
        data_status=lambda: status,
        llm=llm,
    )
    return TestClient(create_app(services))


def stock_list(*, scope=None, metrics=None, as_of=None, top=None, **output) -> dict:
    """一张股票表。top 之外的关键字都进 output（filter / sort / limit）。"""
    spec: dict = {
        "scope": scope or {},
        "subject": {"kind": "pool"},
        "when": {"as_of": as_of or DAY.isoformat()},
        "output": {"kind": "table", **output},
    }
    if metrics is not None:
        spec["metrics"] = metrics
    return spec | (top or {})


def stock_history(event: dict, **output) -> dict:
    return {
        "subject": {"kind": "codes", "codes": ["600519.SH"]},
        "when": {"range": {"from": "2025-01-01", "to": "2025-12-31"}},
        "output": {"kind": "event_study", "event": event, **output},
    }


def issues_of(response) -> dict:
    body = response.json()
    assert response.status_code == 200 and body["status"] == "needs_revision", body
    return {issue["path"]: issue for issue in body["issues"]}


def post(client: TestClient, spec: dict):
    return client.post("/api/run", json={"spec": spec})


# ── /api/run：先看数据够不够，再做确定性检查 ────────────────────


def test_本地数据还不够_返回data_not_ready和同步进度(tmp_path):
    status = replace(READY, ready=False, reason="还没有股票日频数据", data_through=None)
    body = post(make_client(tmp_path, ds=NoData(), status=status), stock_list()).json()
    assert body["status"] == "data_not_ready"
    assert body["message"] == "还没有股票日频数据"
    assert body["data"]["status"]["ready"] is False
    assert body["data"]["sync"]["state"] == "idle"


@pytest.mark.parametrize(
    ("content", "path"),
    [(b"{oops", None), (b"[1]", None), (b"{}", "spec"), (b'{"spec": {}, "extra": 1}', "extra")],
)
def test_请求体写错_返回中文说明而不是422(tmp_path, content, path):
    client = make_client(tmp_path, ds=NoData())
    response = client.post(
        "/api/run", content=content, headers={"content-type": "application/json"}
    )
    assert path in issues_of(response)


def test_结构问题逐条列出_说明是中文(tmp_path):
    spec = stock_list(
        as_of="2026-13-01",
        limit=0,
        metrics=[{"name": "成交额", "expr": "$amount"}],
        sort={"by": "成交额", "order": "up"},
        top={"colour": "red"},
    )
    issues = issues_of(post(make_client(tmp_path, ds=NoData()), spec))
    assert issues["when.as_of"]["message"] == "日期要写成 YYYY-MM-DD"
    assert issues["output.limit"]["message"] == "不能小于 1"
    assert issues["colour"]["message"] == "不认识这一项"
    assert issues["output.sort.order"]["message"] == "只能是 'asc'、'desc'"


def test_不认识的形态(tmp_path):
    issues = issues_of(post(make_client(tmp_path, ds=NoData()), {"output": {"kind": "chart"}}))
    assert "event_study" in issues["output"]["message"]


def test_自己写的校验原样给出(tmp_path):
    spec = stock_history({"preset_id": "limit_up"}) | {
        "when": {"range": {"from": "2025-12-31", "to": "2025-01-01"}}
    }
    issues = issues_of(post(make_client(tmp_path, ds=NoData()), spec))
    assert issues["when.range"]["message"] == "区间的起点 2025-12-31 晚于终点 2025-01-01"


def test_个股回看要用事件库里的事件(tmp_path):
    spec = stock_history({"expr": "$is_limit_up"})
    assert list(issues_of(post(make_client(tmp_path, ds=NoData()), spec))) == [
        "output.event.preset_id"
    ]


def test_事件参数越界_说明可选范围_不连带报表达式缺失(tmp_path):
    spec = stock_history({"preset_id": "breakout_ma", "params": {"ma": 7}})
    issues = issues_of(post(make_client(tmp_path, ds=NoData()), spec))
    assert list(issues) == ["output.event.params.ma"]
    assert issues["output.event.params.ma"]["allowed"]


def test_不认识的事件编号_列出可选(tmp_path):
    spec = stock_history({"preset_id": "no_such_event"})
    issues = issues_of(post(make_client(tmp_path, ds=NoData()), spec))
    assert "limit_up" in issues["output.event.preset_id"]["message"]


def test_表达式写错_指出栏目和位置(tmp_path):
    spec = stock_list(
        metrics=[{"name": "怪指标", "expr": "$no_such_field"}],
        filter={"expr": "$close >"},
        sort={"by": "怪指标"},
    )
    issues = issues_of(post(make_client(tmp_path), spec))
    assert issues["output.filter.expr"]["position"] is not None
    assert issues["metrics.怪指标"]["message"] == "没有字段 $no_such_field"


def test_概念板块不可用时_板块表要换口径(tmp_path):
    spec = stock_list(scope={"target": "concept"})
    assert "scope.target" in issues_of(post(make_client(tmp_path), spec))


def test_日期不是交易日_或者超出本地数据(tmp_path):
    client = make_client(tmp_path)
    weekend = issues_of(post(client, stock_list(as_of="2026-09-06")))
    assert weekend["when.as_of"]["message"] == "2026-09-06 不是交易日"
    later = issues_of(post(client, stock_list(as_of="2026-09-14")))
    assert later["when.as_of"]["message"] == "本地股票数据只覆盖 2016-01-04 ~ 2026-09-11"


def test_行业名不存在_列出可选的行业(tmp_path):
    issues = issues_of(post(make_client(tmp_path), stock_list(scope={"industry": "银行业"})))
    assert issues["scope.industry"]["allowed"] == "农林牧渔、银行"


def test_可选值用顿号隔开_成本不收NaN(tmp_path):
    client = make_client(tmp_path, ds=NoData())
    spec = stock_history({"preset_id": "limit_up"}, benchmark="hs300")
    issues = issues_of(post(client, spec))
    expected = "只能是 'universe_equal_weight'、'index:000300.SH'、'index:000905.SH'"
    assert issues["output.benchmark"]["message"] == expected

    body = json.dumps({"spec": stock_history({"preset_id": "limit_up"}, cost_bps=float("nan"))})
    response = client.post("/api/run", content=body, headers={"content-type": "application/json"})
    assert issues_of(response)["output.cost_bps"]["message"] == "要是有限的数字"


# ── /api/run：计算与运行记录 ────────────────────────────────────


def test_算完存运行记录_按编号取回(tmp_path, monkeypatch):
    row = {"code": "600519.SH", "成交额": float("nan")}
    result = ListResult(
        as_of=DAY,
        total=1,
        pool_size=2,
        head=("code",),
        columns=(Column("成交额", "元"),),
        rows=(row,),
        notes=("按成交额排",),
    )
    monkeypatch.setattr("litmus.api.routes.runs.run_research", lambda spec, ds: result)
    client = make_client(tmp_path)

    body = client.post("/api/run", json={"spec": stock_list(), "plan_id": "p1"}).json()
    assert body["status"] == "done"
    assert body["result"]["as_of"] == "2026-09-11"
    assert body["result"]["rows"] == [{"code": "600519.SH", "成交额": None}]  # NaN 不是合法 JSON
    assert body["result"]["kind"] == "table"

    record = client.get(f"/api/run/{body['run_id']}").json()
    assert record["result"] == body["result"]
    assert (record["status"], record["plan_id"], record["data_through"]) == (
        "done",
        "p1",
        "2026-09-11",
    )
    assert record["spec"]["output"]["kind"] == "table" and record["duration_ms"] >= 0


def test_计算出错_存下失败记录_返回编号(tmp_path, monkeypatch):
    def boom(spec, ds):
        raise RuntimeError("炸了")

    monkeypatch.setattr("litmus.api.routes.runs.run_research", boom)
    client = make_client(tmp_path)
    body = post(client, stock_list()).json()
    assert body["status"] == "failed" and body["run_id"] in body["message"]
    record = client.get(f"/api/run/{body['run_id']}").json()
    assert (record["status"], record["error"], record["result"]) == (
        "failed",
        "RuntimeError: 炸了",
        None,
    )


def test_计算时才发现数据缺口_让用户改条件_不存记录(tmp_path, monkeypatch):
    def missing(spec, ds):
        raise MissingDataError("本地股票日频缺 2020-03")

    monkeypatch.setattr("litmus.api.routes.runs.run_research", missing)
    issues = issues_of(post(make_client(tmp_path), stock_list()))
    assert issues[None]["message"] == "本地股票日频缺 2020-03"
    assert not list(tmp_path.rglob("*.json"))


def test_取不存在的运行记录返回404(tmp_path):
    response = make_client(tmp_path, ds=NoData()).get("/api/run/r20260914000000abcdef")
    assert response.status_code == 404


# ── 清单 ────────────────────────────────────────────────────────


def test_事件清单带版本号(tmp_path):
    body = make_client(tmp_path, ds=NoData()).get("/api/events").json()
    assert body["library_version"] == EVENTS.version
    assert [event["id"] for event in body["events"]] == [event.id for event in EVENTS.events]


def test_板块清单(tmp_path):
    client = make_client(tmp_path)
    boards = client.get("/api/boards?type=sw_industry").json()["boards"]
    assert boards[1] == {"code": "801780.SI", "name": "银行"}
    assert client.get("/api/boards?type=concept").status_code == 409
    assert client.get("/api/boards?type=industry").status_code == 400
    assert client.get("/api/boards").status_code == 400


# ── 同步 ────────────────────────────────────────────────────────


class GatedSync:
    """日频那一步等 gate 放行才落盘一个月。"""

    def __init__(self):
        self.gate = threading.Event()

    def sync_all(self, start, end, manifest=None, steps=None, on_month=None):
        if steps == ["daily"]:
            self.gate.wait(timeout=5)
            on_month(MonthResult("2026-08", 100, 21, (), True), 1, 2)
            on_month(MonthResult("2026-09", 100, 8, (), False), 2, 2)
        return {}


def test_数据状态带上各类数据截至哪天(tmp_path):
    body = make_client(tmp_path).get("/api/data/status").json()
    assert body["latest"] == [
        {"key": "stock", "label": "股票行情", "date": "2026-09-11"},
        {"key": "sw_industry", "label": "申万行业行情", "date": "2026-09-11"},
    ]


def test_触发同步_不重复开_停止_查看进度(tmp_path):
    fake = GatedSync()

    @contextmanager
    def opener():
        yield fake

    job = SyncJob(opener)
    client = make_client(tmp_path, job=job)  # 查进度会顺带读各类数据截至哪天

    body = client.post("/api/data/sync").json()
    assert body["started"] is True and body["sync"]["state"] == "running"
    assert client.post("/api/data/sync").json()["started"] is False
    assert client.post("/api/data/sync/stop").json()["stopped"] is True
    fake.gate.set()
    assert job.wait(5)

    body = client.get("/api/data/status").json()
    assert body["sync"]["state"] == "stopped"
    assert body["status"]["ready"] is True


# ── 字段、板块区间、K 线 ────────────────────────────────────────


def test_字段清单按标的类型分_带中文名和单位(tmp_path):
    fields = make_client(tmp_path, ds=NoData()).get("/api/fields").json()["fields"]
    assert set(fields) == {"stock", "sw_industry", "sw_industry_l2", "concept"}
    close = next(item for item in fields["stock"] if item["name"] == "$amount")
    assert (close["label"], close["unit"]) == ("成交额", "元")
    assert all(item["name"] != "$close_raw" for item in fields["sw_industry"])


def test_板块清单带数据可用区间(tmp_path):
    body = make_client(tmp_path).get("/api/boards?type=sw_industry").json()
    assert body["range"] == ["2016-01-04", "2026-09-11"]


def test_事件清单带参数类型和范围(tmp_path):
    events = make_client(tmp_path, ds=NoData()).get("/api/events").json()["events"]
    ma = next(e for e in events if e["id"] == "breakout_ma")["params"][0]
    assert (ma["kind"], ma["choices"], ma["min"], ma["max"]) == (
        "choice",
        [5, 10, 20, 60, 120, 250],
        None,
        None,
    )


@pytest.mark.parametrize(
    "query", ["from=20260901&to=2026-09-11", "from=2026-09-11&to=2026-09-01", "to=2026-09-11"]
)
def test_K线参数写错不读数据直接400(tmp_path, query):
    response = make_client(tmp_path, ds=NoData()).get(f"/api/stocks/600519.SH/kline?{query}")
    assert response.status_code == 400


# ── 只检查不计算、说明文字、找股票 ──────────────────────────────


def test_检查接口_不计算_给出说明文字_没给的栏目标成默认值(tmp_path, monkeypatch):
    def no_run(spec, ds):
        raise AssertionError("检查接口不该计算")

    monkeypatch.setattr("litmus.api.routes.runs.run_research", no_run)
    body = make_client(tmp_path).post("/api/check", json={"spec": stock_list()}).json()
    assert body["status"] == "ok", body
    # 日期是请求里给的，不在默认值里
    assert body["spec"]["defaults_used"] == [
        "scope.base",
        "scope.exclude",
        "output.limit",
        "output.sort",
    ]
    items = {item["field"]: item for item in body["assumptions"]}
    assert items["output.sort"] == {
        "group": "怎么出",
        "field": "output.sort",
        "text": "排序：按「成交额」从高到低",
        "default": True,
    }
    # 日期这次是请求里给的，不标默认值
    assert (items["when.as_of"]["text"], items["when.as_of"]["default"]) == (
        "日期：2026-09-11",
        False,
    )
    assert body["summary"].startswith("沪深A股（不含北交所）里")
    assert body["spec"]["assumptions"] == [item["text"] for item in body["assumptions"]]


def test_检查接口_没给日期用最近交易日_标成默认值(tmp_path):
    spec = {"output": {"kind": "table"}}
    body = make_client(tmp_path).post("/api/check", json={"spec": spec}).json()
    assert body["status"] == "ok", body
    assert body["spec"]["when"]["as_of"] == "2026-09-11"
    assert body["spec"]["defaults_used"][0] == "when"
    items = {item["field"]: item for item in body["assumptions"]}
    day = items["when.as_of"]
    assert (day["group"], day["text"], day["default"]) == ("看哪天", "日期：2026-09-11", True)


def test_检查接口_请求里带来的说明文字和默认值标记不作数(tmp_path):
    spec = stock_list(limit=10, top={"assumptions": ["乱写的说明"], "defaults_used": ["when"]})
    body = make_client(tmp_path).post("/api/check", json={"spec": spec}).json()
    assert "乱写的说明" not in body["spec"]["assumptions"]
    assert {"when", "output.limit"}.isdisjoint(body["spec"]["defaults_used"])


def test_检查接口_要改的照样返回问题_不读数据(tmp_path):
    spec = stock_history({"preset_id": "breakout_ma", "params": {"ma": 7}})
    body = make_client(tmp_path, ds=NoData()).post("/api/check", json={"spec": spec}).json()
    assert body["status"] == "needs_revision"
    assert body["issues"][0]["path"] == "output.event.params.ma"


def test_运行记录里存的是代码生成的说明文字(tmp_path, monkeypatch):
    result = ListResult(as_of=DAY, total=0, pool_size=0, head=("code",), columns=(), rows=())
    monkeypatch.setattr("litmus.api.routes.runs.run_research", lambda spec, ds: result)
    client = make_client(tmp_path)
    body = post(client, stock_list(top={"assumptions": ["乱写的说明"]})).json()
    record = client.get(f"/api/run/{body['run_id']}").json()
    assert "日期：2026-09-11" in record["spec"]["assumptions"]
    assert "乱写的说明" not in record["spec"]["assumptions"]


def test_找股票要给关键词(tmp_path):
    client = make_client(tmp_path, ds=NoData())
    assert client.get("/api/stocks?q=%20").status_code == 400
    assert client.get("/api/stocks").status_code == 400


# ── 提问 ────────────────────────────────────────────────────────


class NoLLM(LLMClient):
    def structured(self, system, user, schema):
        raise AssertionError("这个请求不该调大模型")


def test_提问_本地数据不够时不调大模型(tmp_path):
    status = replace(READY, ready=False, reason="还没有股票日频数据")
    client = make_client(tmp_path, ds=NoData(), status=status, llm=NoLLM())
    body = client.post("/api/plan", json={"query": "昨天涨停的股票"}).json()
    assert (body["status"], body["message"]) == ("data_not_ready", "还没有股票日频数据")


def test_提问_大模型没配好时说明原因(tmp_path):
    body = (
        make_client(tmp_path, ds=NoData())
        .post("/api/plan", json={"query": "昨天涨停的股票"})
        .json()
    )
    assert body["status"] == "failed" and "大模型没有配置好" in body["message"]


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({}, "query 要填问题"),
        ({"query": "  "}, "query 要填问题"),
        ({"query": "x", "extra": 1}, "不认识的栏目"),
        ({"query": "问" * 501}, "问题太长"),
        ({"query": "x", "previous_plan_id": 5}, "previous_plan_id"),
        ({"query": "x", "spec": [1]}, "spec 要是一个对象"),
    ],
)
def test_提问_请求体写错返回400(tmp_path, payload, detail):
    response = make_client(tmp_path, ds=NoData(), llm=NoLLM()).post("/api/plan", json=payload)
    assert response.status_code == 400 and detail in response.json()["detail"]


def test_页面开关_默认开_关掉只是不显示(tmp_path):
    assert make_client(tmp_path).get("/api/settings").json() == {"show_trace": True}

    services = Services(
        ds=FakeData(),
        store=JsonStore(tmp_path),
        events=EVENTS,
        sync_job=SyncJob(no_sync),
        data_status=lambda: READY,
        show_trace=False,
    )
    off = TestClient(create_app(services))
    assert off.get("/api/settings").json() == {"show_trace": False}
    # 关掉只是页面不显示，接口照样取得到
    assert off.get("/api/traces/p20260916-21-37-20aaaaaa").status_code == 404


def test_页面开关_环境变量怎么解读(monkeypatch):
    from litmus.api.services import _flag

    for value in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv("LITMUS_SHOW_TRACE", value)
        assert _flag("LITMUS_SHOW_TRACE", default=False) is True, value
    for value in ("0", "false", "no", "off", "随便写"):
        monkeypatch.setenv("LITMUS_SHOW_TRACE", value)
        assert _flag("LITMUS_SHOW_TRACE", default=True) is False, value
    monkeypatch.setenv("LITMUS_SHOW_TRACE", "   ")  # 没填就用默认
    assert _flag("LITMUS_SHOW_TRACE", default=True) is True
    monkeypatch.delenv("LITMUS_SHOW_TRACE")
    assert _flag("LITMUS_SHOW_TRACE", default=True) is True


def test_过程_接口能按提问编号取回_不存在返回404(tmp_path):
    from litmus.store import PlanRecord

    store = JsonStore(tmp_path)
    plan_id = store.save_plan(PlanRecord(query="x", status="ok", steps=[{"step": "llm.plan"}]))

    client = make_client(tmp_path, ds=NoData(), llm=NoLLM())
    body = client.get(f"/api/traces/{plan_id}").json()
    assert body["record_id"] == plan_id and body["steps"] == [{"step": "llm.plan"}]
    assert body["query"] == "x" and body["created_at"]
    assert client.get("/api/traces/p20260916-21-37-20aaaaaa").status_code == 404
    assert client.get("/api/traces/..%2Fsecret").status_code == 404


def test_提问_追问的提问记录不存在(tmp_path):
    client = make_client(tmp_path, ds=NoData(), llm=NoLLM())
    payload = {"query": "20 个交易日", "previous_plan_id": "p20260915000000abcdef"}
    body = client.post("/api/plan", json=payload).json()
    assert body["status"] == "failed" and "p20260915000000abcdef" in body["message"]


def test_原话的说法_改过的栏目不再用_股票只比代码():
    from litmus.api.explain import plan_mentions
    from litmus.store import PlanRecord

    mentions = [
        {"phrase": "昨天", "field": "when.as_of"},
        {"phrase": "茅台", "field": "subject"},
        {"phrase": "前 20", "field": "output.limit"},
    ]
    saved = {
        "when": {"as_of": "2026-09-11"},
        "subject": {"kind": "codes", "codes": ["600519.SH"]},
    }
    record = PlanRecord(query="问题", status="ok", spec=saved, detail={"mentions": mentions})
    edited = {
        "when": {"as_of": "2026-09-10"},
        "subject": {"kind": "codes", "codes": ["600519.SH"]},
        "output": {"limit": 20},
    }
    # 日期改过了不再用「昨天」；股票代码没变照样用「茅台」；提问时没定下来的「前 20」照样用
    assert [m.phrase for m in plan_mentions(edited, record)] == ["茅台", "前 20"]
    assert plan_mentions(edited, None) == []


def test_确认卡只列这个问题用到的数据():
    from litmus.api.explain import _used_data
    from litmus.spec import parse_spec

    assert _used_data(parse_spec(stock_list()), ["$pct_chg > 9"]) == {"stock"}
    hs300 = parse_spec(stock_list(scope={"base": "hs300"}))
    assert _used_data(hs300, ["$roe > 10"]) == {"stock", "finance", "index_weight"}
    board = parse_spec(stock_list(scope={"target": "sw_industry"}))
    assert _used_data(board, []) == {"sw_industry"}


def test_条件用了默认门槛_原话没给数字才标默认值():
    from litmus.api.explain import _defaulted
    from litmus.spec import Mention

    expr = "$market_cap < 30亿 & $is_limit_up"
    small = (Mention("小市值", "output.filter"),)
    assert _defaulted({}, expr, small) == {"output.filter"}
    # 同一栏里别的说法带数字（说的是市盈率），小市值照样是默认门槛
    both = (*small, Mention("市盈率低于 20", "output.filter"))
    assert _defaulted({}, expr, both) == {"output.filter"}
    assert _defaulted({}, expr, (Mention("市值低于 30 亿", "output.filter"),)) == frozenset()
    assert _defaulted({}, expr, (Mention("30亿以下", "output.filter"),)) == frozenset()
    assert _defaulted({}, expr, ()) == frozenset()  # 手填的、确认卡上改过的
    assert _defaulted({}, "$market_cap < 50亿", small) == frozenset()
    # 指标上的默认门槛同样标出来
    metric = {"小市值股票": expr}
    assert _defaulted(metric, "", (Mention("小市值", "metrics.小市值股票"),)) == {
        "metrics.小市值股票"
    }


def test_候选里只有一个代码或名称完全一致的_直接用():
    from litmus.api.routes.plan import _decisive

    exact = SimpleNamespace(exact=True, code="601318.SH")
    loose = SimpleNamespace(exact=False, code="000001.SZ")
    assert _decisive([exact, loose]) == [exact]
    assert _decisive([loose, loose]) == [loose, loose]
    assert _decisive([exact, exact]) == [exact, exact]


def test_板块口径规则_完全对上一个才直接用_其余让用户选():
    from litmus.api.routes.plan import _pick_board
    from litmus.data import BoardMatch
    from litmus.llm import NameMention

    name_rule, contains_rule = 2, 3
    table = {
        "半导体板块": [
            BoardMatch("801081.SI", "半导体", "sw_industry_l2", name_rule),
            BoardMatch("880608.TDX", "第三代半导体", "concept", contains_rule),
        ],
        "半导体": [BoardMatch("880608.TDX", "第三代半导体", "concept", contains_rule)],
        "芯片": [BoardMatch("880500.TDX", "芯片", "concept", name_rule)],
        "光通信": [BoardMatch("880670.TDX", "光通信", "concept", name_rule)],
        "CPO概念": [BoardMatch("880656.TDX", "CPO概念", "concept", name_rule)],
    }
    ds = SimpleNamespace(resolve_board=lambda text: table.get(text, []))

    def pick(mention, guess=None):
        picked, candidates = _pick_board(ds, NameMention(mention, guess))
        return (picked and picked.name), [c.name for c in candidates]

    assert pick("半导体板块", "芯片") == ("半导体", [])  # 原话完全对上一个
    # 原话只是名称包含对上：不直接用，连同猜的一起让用户选
    assert pick("半导体", "芯片") == (None, ["第三代半导体", "芯片"])
    assert pick("光模块", "光通信") == ("光通信", [])  # 原话没对上，猜的对上一个
    assert pick("光模块", "光通信、CPO概念") == (None, ["光通信", "CPO概念"])


def test_股票池的行业写明是几级():
    from litmus.api.explain import _industry_scope

    assert _industry_scope("银行", FakeData()) == "申万一级行业"
    assert _industry_scope("半导体", FakeData()) == ""  # 这个替身没有二级
