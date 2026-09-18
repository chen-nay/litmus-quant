"""HTTP 接口在本地真实数据上跑出三种结果（第 5 步验收标准）。

数据量都很小：单日股票表、单日申万行业表、一只股票一年的回看。运行记录写进临时目录，不动 data/store；
不同步、不连 Tushare。没有本地数据就整个跳过。
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from litmus.api import Services, SyncJob, create_app
from litmus.data import DataService, DataSync, MarketStore, MissingDataError
from litmus.signals import load_events
from litmus.store import JsonStore

DAY = date(2026, 9, 11)
_market = MarketStore.from_env()
try:
    _first, _last = DataService(_market).data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的股票日频数据", allow_module_level=True)
if _first > date(2025, 1, 2) or _last < DAY:
    pytest.skip(
        f"用例要 2025-01-02 ~ {DAY} 的数据，本地只有 {_first} ~ {_last}", allow_module_level=True
    )


def no_sync():
    raise AssertionError("契约测试不同步")


@pytest.fixture
def client(tmp_path) -> TestClient:
    services = Services(
        ds=DataService(_market),
        store=JsonStore(tmp_path),
        events=load_events(),
        sync_job=SyncJob(no_sync),
        data_status=DataSync(None, _market).status,
    )
    return TestClient(create_app(services))


def run(client: TestClient, spec: dict) -> dict:
    return client.post("/api/run", json={"spec": spec}).json()


def test_股票表(client):
    spec = {
        "subject": {"kind": "pool"},
        "when": {"as_of": DAY.isoformat()},
        "metrics": [{"name": "成交额", "expr": "$amount"}],
        "output": {
            "kind": "table",
            "filter": {"expr": "$pct_chg > 9"},
            "sort": {"by": "成交额"},
            "limit": 5,
        },
    }
    body = run(client, spec)
    assert body["status"] == "done", body
    result = body["result"]
    assert (result["kind"], result["as_of"]) == ("table", "2026-09-11")
    assert 0 < len(result["rows"]) == min(5, result["total"])
    assert all(row["name"] for row in result["rows"])
    amounts = [row["成交额"] for row in result["rows"]]
    assert amounts == sorted(amounts, reverse=True)
    assert [c["name"] for c in result["columns"]] == ["成交额"]
    assert result["understood"].startswith("沪深A股（不含北交所）里") and result["assumptions"]
    assert client.get(f"/api/run/{body['run_id']}").json()["result"] == result


def test_板块表(client):
    spec = {
        "scope": {"target": "sw_industry"},
        "subject": {"kind": "pool"},
        "when": {"as_of": DAY.isoformat()},
        "metrics": [{"name": "涨跌幅", "expr": "$pct_chg"}],
        "output": {"kind": "table", "sort": {"by": "涨跌幅"}, "limit": 3},
    }
    body = run(client, spec)
    assert body["status"] == "done", body
    assert len(body["result"]["rows"]) == 3
    assert body["result"]["total"] == 31


def test_卡_结果带着卡底下的怎么算的(client):
    spec = {
        "scope": {"target": "stock", "industry": "农林牧渔"},
        "subject": {"kind": "codes", "codes": ["002714.SZ"]},
        "when": {"as_of": DAY.isoformat()},
        "metrics": [{"name": "今年以来涨幅排名", "expr": "Rank(PctSince($close, 20251231))"}],
        "output": {"kind": "card"},
    }
    body = run(client, spec)
    assert body["status"] == "done", body
    result = body["result"]
    assert (result["kind"], result["as_of"]) == ("card", DAY.isoformat())
    item = result["items"][0]
    assert (item["code"], item["name"]) == ("002714.SZ", "牧原股份")
    row = item["rows"][0]
    assert row["name"] == "今年以来涨幅排名" and "名（共" in row["text"] and row["note"]

    assert result["understood"] == "牧原股份的今年以来涨幅排名"
    texts = [item["text"] for item in result["assumptions"]]
    assert texts[0].startswith("算的范围：农林牧渔")
    assert not any(text.startswith("股票：") for text in texts)  # 标题上就是它

    trace = client.get(f"/api/traces/{body['run_id']}").json()
    assert {step["step"]: step for step in trace["steps"]}["respond"]["cards"] == 1


def test_卡要小结但没配大模型_卡照样出_没有小结(client):
    spec = {
        "subject": {"kind": "codes", "codes": ["002714.SZ"]},
        "when": {"as_of": DAY.isoformat()},
        "metrics": [{"name": "市净率", "expr": "$pb"}],
        "output": {"kind": "card"},
        "narrate": True,
    }
    body = run(client, spec)
    assert body["status"] == "done", body
    assert body["result"]["narrate"] is False and body["result"]["items"]  # 页面不占小结的位置
    narrative = client.post(f"/api/run/{body['run_id']}/narrative").json()
    assert narrative == {"text": "", "error": "大模型没有配置好"}
    assert client.post("/api/run/r20260101-00-00-00abcdef/narrative").status_code == 404


def test_个股回看_事件按事件库重新生成(client):
    spec = {
        "subject": {"kind": "codes", "codes": ["000001.SZ"]},
        "when": {"range": {"from": "2025-01-01", "to": "2025-12-31"}},
        "output": {
            "kind": "event_study",
            "event": {"preset_id": "breakout_ma", "params": {"ma": 250}, "expr": "$close > 0"},
            "horizons": [5],
        },
    }
    body = run(client, spec)
    assert body["status"] == "done", body
    result = body["result"]
    assert (result["kind"], result["code"]) == ("event_study", "000001.SZ")
    assert list(result["summary"]) == ["5"]  # 持有天数做 key，JSON 里是字符串

    record = client.get(f"/api/run/{body['run_id']}").json()
    event = record["spec"]["output"]["event"]
    assert "250" in event["expr"] and event["expr"] != "$close > 0"  # 请求里带来的表达式不作数
    assert record["library_version"] == event["library_version"] == load_events().version
    assert result["event_label"] == event["label"]


def test_过程记录_运行链每一步的耗时和结果规模(client):
    """2026-09-16 加：以前只有一个总耗时，看不出慢在生成说明还是算结果。"""
    spec = {
        "subject": {"kind": "pool"},
        "when": {"as_of": DAY.isoformat()},
        "metrics": [{"name": "成交额", "expr": "$amount"}],
        "output": {
            "kind": "table",
            "filter": {"expr": "$pct_chg > 9"},
            "sort": {"by": "成交额"},
            "limit": 5,
        },
    }
    body = run(client, spec)
    assert body["status"] == "done", body

    trace = client.get(f"/api/traces/{body['run_id']}").json()
    assert trace["record_id"] == body["run_id"] and trace["query"] == "table"
    steps = {step["step"]: step for step in trace["steps"]}
    assert list(steps) == ["check_spec", "explain", "research.run", "respond"]

    assert steps["check_spec"]["issues"] == [] and steps["check_spec"]["kind"] == "table"
    assert steps["explain"]["assumptions"] > 0 and steps["explain"]["ms"] >= 0
    assert steps["research.run"]["ms"] >= 0
    done = steps["respond"]
    assert done["status"] == "done"
    assert done["total"] == body["result"]["total"] and done["rows"] == len(body["result"]["rows"])


def test_过程记录_个股回看记的是触发了几次(client):
    spec = {
        "subject": {"kind": "codes", "codes": ["600519.SH"]},
        "when": {"range": {"from": "2025-01-02", "to": DAY.isoformat()}},
        "output": {
            "kind": "event_study",
            "event": {"preset_id": "breakout_ma", "params": {"ma": 250}},
            "horizons": [5],
        },
    }
    body = run(client, spec)
    assert body["status"] == "done", body
    trace = client.get(f"/api/traces/{body['run_id']}").json()
    done = next(s for s in trace["steps"] if s["step"] == "respond")
    assert done["triggers"] == len(body["result"]["triggers"])


def test_查不到的东西让用户改(client):
    spec = {
        "scope": {"industry": "银行业"},
        "when": {"as_of": "2026-09-06"},
        "output": {"kind": "table"},
    }
    issues = {issue["path"]: issue for issue in run(client, spec)["issues"]}
    assert issues["when.as_of"]["message"] == "2026-09-06 不是交易日"
    assert "银行" in issues["scope.industry"]["allowed"].split("、")

    spec = {
        "subject": {"kind": "codes", "codes": ["600519.XX"]},
        "when": {"range": {"from": "2025-01-01", "to": "2025-03-31"}},
        "output": {"kind": "event_study", "event": {"preset_id": "limit_up"}},
    }
    body = run(client, spec)
    assert body["status"] == "needs_revision"
    assert [issue["path"] for issue in body["issues"]] == ["subject.codes"]


def test_数据状态与板块清单(client):
    body = client.get("/api/data/status").json()
    assert body["status"]["ready"] is True
    assert body["status"]["data_through"] >= DAY.isoformat()
    assert body["sync"]["state"] == "idle"
    assert len(client.get("/api/boards?type=sw_industry").json()["boards"]) == 31


def test_检查接口_个股回看的实际统计起点和结果对得上(client):
    """年线要往前读 250 多条行情，本地数据从 2016-01-04 起，回看实际从 2017 年初才算得出来。"""
    spec = {
        "subject": {"kind": "codes", "codes": ["000001.SZ"]},
        "when": {"range": {"from": "2016-01-01", "to": "2017-06-30"}},
        "output": {
            "kind": "event_study",
            "event": {"preset_id": "breakout_ma"},
            "horizons": [5],
        },
    }
    body = client.post("/api/check", json={"spec": spec}).json()
    assert body["status"] == "ok", body
    items = {item["field"]: item for item in body["assumptions"]}
    assert items["subject"]["text"] == "股票：平安银行（000001.SZ）"
    assert items["output.event"]["default"] is True  # 均线天数没给，用了默认值
    assert items["output.benchmark"]["default"] is True

    run = client.post("/api/run", json={"spec": spec}).json()
    assert run["status"] == "done", run
    first = run["result"]["range"][0]
    assert first.startswith("2017-")
    assert f"实际从 {first} 算起" in items["when.range"]["text"]


def test_检查接口_表达式翻成中文(client):
    spec = {
        "subject": {"kind": "pool"},
        "when": {"as_of": DAY.isoformat()},
        "output": {
            "kind": "table",
            "filter": {"expr": "$amount > Mean(Ref($amount, 1), 20) * 2"},
            "limit": 5,
        },
    }
    body = client.post("/api/check", json={"spec": spec}).json()
    items = {item["field"]: item["text"] for item in body["assumptions"]}
    assert items["output.filter"] == "先筛：成交额 > 前 20 日成交额均值（不含当天） × 2"


def test_找股票(client):
    body = client.get("/api/stocks?q=平安").json()
    assert {"000001.SZ", "601318.SH"} <= {match["code"] for match in body["matches"]}
    first = client.get("/api/stocks?q=gzmt").json()["matches"][0]
    assert (first["code"], first["rule"]) == ("600519.SH", "拼音首字母")
