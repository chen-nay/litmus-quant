"""研究记录存储的契约测试（ARCHITECTURE §7）。写到临时目录，不需要任何数据。

以后换 SQLite 实现，同一套测试照样要过。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from litmus.store import JsonStore, NarrativeRecord, PlanRecord, RunRecord, TraceRecord


@pytest.fixture
def store(tmp_path: Path) -> JsonStore:
    return JsonStore(tmp_path)


RUN = RunRecord(
    spec={"shape": "stock_list", "as_of": "2026-09-11", "limit": 5},
    status="done",
    result={
        "total": 13,
        "rows": [{"code": "603421.SH", "name": "鼎信通讯"}],
        "summary": {"5": 0.1},
    },
    data_through="2026-09-11",
    library_version=1,
    duration_ms=812,
)


def test_运行记录存了能原样取回_编号和时间由存储填(store):
    run_id = store.save_run(RUN)
    again = store.get_run(run_id)
    assert again is not None
    assert again.run_id == run_id and again.created_at
    assert again.spec == RUN.spec and again.result == RUN.result  # 中文和嵌套结构原样保留
    assert (again.status, again.data_through, again.library_version, again.duration_ms) == (
        "done",
        "2026-09-11",
        1,
        812,
    )


def test_提问记录存了能取回(store):
    plan_id = store.save_plan(
        PlanRecord(query="昨天哪些股票放量？", status="ok", spec={"shape": "stock_list"})
    )
    plan = store.get_plan(plan_id)
    assert plan is not None and plan.query == "昨天哪些股票放量？" and plan.plan_id == plan_id


#: 编号：类型字母 + 日期 + -时-分-秒 + 随机后缀，如 r20260914-15-30-12a1b2c3
ID = r"\d{8}-\d{2}-\d{2}-\d{2}[0-9a-f]{6}"


def test_编号格式_时间部分按保存先后递增_同一秒也不撞(store):
    ids = [store.save_run(RUN) for _ in range(3)]
    assert all(re.fullmatch(f"r{ID}", run_id) for run_id in ids)
    assert len(set(ids)) == 3
    # 位宽固定，所以按字符串排序就是按时间排序；同一秒内的几条之间不保证先后
    stamps = [run_id[1:18] for run_id in ids]
    assert stamps == sorted(stamps)
    assert re.fullmatch(f"p{ID}", store.save_plan(PlanRecord(query="x", status="failed")))


def test_查不到或编号不合法返回空_不会读到目录外面(store, tmp_path):
    (tmp_path / "secret.json").write_text('{"spec": {}, "status": "done"}', encoding="utf-8")
    assert store.get_run("r20260914-15-30-12a1b2c3") is None
    assert store.get_run("../secret") is None
    assert store.get_run("p20260914-15-30-12a1b2c3") is None  # 提问记录的编号拿去当运行记录取
    assert store.get_run("r20260914153012a1b2c3") is None  # 旧格式不认
    assert store.get_plan("") is None
    assert store.get_trace("../secret") is None


def test_一条记录一个文件_不留临时文件(store, tmp_path):
    run_id = store.save_run(RUN)
    plan_id = store.save_plan(PlanRecord(query="x", status="ok"))
    store.save_trace(TraceRecord(record_id=plan_id))
    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    # 过程记录的文件名是 t + 它记录的那条编号：一眼看出记的是提问还是运行
    assert files == [f"plans/{plan_id}.json", f"runs/{run_id}.json", f"traces/t{plan_id}.json"]


def test_过程记录_编号就是它记录的那条_提问和运行都能挂(store):
    plan_id = store.save_plan(PlanRecord(query="牧原股份现在市盈率多少？", status="unsupported"))
    steps = [{"step": "llm.plan", "attempt": 1, "raw_reply": {"status": "unsupported"}}]
    assert store.save_trace(TraceRecord(record_id=plan_id, query="x", steps=steps)) == plan_id

    trace = store.get_trace(plan_id)
    assert trace is not None
    assert trace.record_id == plan_id and trace.created_at
    assert trace.steps == steps  # 嵌套结构原样取回，store 不认识里面是什么

    run_id = store.save_run(RUN)
    assert store.save_trace(TraceRecord(record_id=run_id)) == run_id
    assert store.get_trace(run_id) is not None


def test_过程记录的编号必须是合法的提问或运行编号(store):
    with pytest.raises(ValueError, match="plan_id 或 run_id"):
        store.save_trace(TraceRecord(record_id="../secret"))
    assert store.get_trace("p20260914-15-30-12a1b2c3") is None  # 合法但不存在
    assert store.get_trace("../secret") is None
    assert store.get_trace("t20260914-15-30-12a1b2c3") is None  # 落盘名拿来当编号查不到


def test_小结_编号就是那次运行的_只能挂在运行上(store):
    run_id = store.save_run(RUN)
    assert store.get_narrative(run_id) is None  # 还没写
    assert store.save_narrative(NarrativeRecord(run_id=run_id, text="在跌")) == run_id
    narrative = store.get_narrative(run_id)
    assert narrative is not None and narrative.created_at
    assert (narrative.text, narrative.error) == ("在跌", None)

    plan_id = store.save_plan(PlanRecord(query="问题", status="ok"))
    with pytest.raises(ValueError, match="run_id"):
        store.save_narrative(NarrativeRecord(run_id=plan_id, text=""))
    assert store.get_narrative("../secret") is None


def test_转不了JSON的内容直接报错_不留残缺文件(store, tmp_path):
    with pytest.raises(TypeError):
        store.save_run(RunRecord(spec={"when": object()}, status="done"))
    assert not any(tmp_path.rglob("*.json")) and not any(tmp_path.rglob("*.tmp"))
