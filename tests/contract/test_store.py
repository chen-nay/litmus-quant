"""研究记录存储的契约测试（ARCHITECTURE §7）。写到临时目录，不需要任何数据。

以后换 SQLite 实现，同一套测试照样要过。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from litmus.store import JsonStore, Narrative, PlanRecord, RunRecord


@pytest.fixture
def store(tmp_path: Path) -> JsonStore:
    return JsonStore(tmp_path)


RUN = RunRecord(
    spec={"version": 2, "when": {"as_of": "2026-09-11"}, "output": {"kind": "table", "limit": 5}},
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
        PlanRecord(query="昨天哪些股票放量？", status="ok", spec={"version": 2})
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


def test_一件事一个文件_只有提问和运行两种_不留临时文件(store, tmp_path):
    run_id = store.save_run(RUN)
    plan_id = store.save_plan(PlanRecord(query="x", status="ok"))
    store.add_steps(plan_id, [{"step": "respond"}])
    store.link_run(plan_id, run_id)
    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert files == [f"plans/{plan_id}.json", f"runs/{run_id}.json"]


def test_过程写进它属于的那条记录_提问和运行都能写(store):
    plan_id = store.save_plan(PlanRecord(query="牧原股份现在市盈率多少？", status="unsupported"))
    steps = [{"step": "llm.plan", "attempt": 1, "raw_reply": {"status": "unsupported"}}]
    store.add_steps(plan_id, steps)
    store.add_steps(plan_id, [{"step": "respond", "status": "unsupported"}])

    plan = store.get_plan(plan_id)
    assert plan is not None
    # 嵌套结构原样取回，store 不认识里面是什么；两次写的接在一起
    assert plan.steps == [*steps, {"step": "respond", "status": "unsupported"}]

    run_id = store.save_run(RUN)
    store.add_steps(run_id, [{"step": "research.run", "ms": 12}])
    run = store.get_run(run_id)
    assert run is not None and run.steps == [{"step": "research.run", "ms": 12}]


def test_写进不存在的记录直接报错_编号不合法也报错(store):
    with pytest.raises(ValueError, match="编号"):
        store.add_steps("../secret", [{"step": "x"}])
    with pytest.raises(ValueError, match="没有编号"):
        store.add_steps("p20260914-15-30-12a1b2c3", [{"step": "x"}])  # 合法但不存在
    with pytest.raises(ValueError, match="编号"):
        store.set_narrative(store.save_plan(PlanRecord(query="x", status="ok")), Narrative(""))


def test_一次提问引出的运行都挂在它名下_重复挂不记两遍(store):
    plan_id = store.save_plan(PlanRecord(query="问题", status="ok"))
    first, second = store.save_run(RUN), store.save_run(RUN)
    store.link_run(plan_id, first)
    store.link_run(plan_id, second)
    store.link_run(plan_id, first)
    plan = store.get_plan(plan_id)
    assert plan is not None and plan.runs == [first, second]


def test_小结写进那次运行的记录里(store):
    run_id = store.save_run(RUN)
    assert store.get_run(run_id).narrative is None  # 还没写
    store.set_narrative(run_id, Narrative(text="在跌"))
    run = store.get_run(run_id)
    assert run is not None and run.narrative is not None
    assert (run.narrative.text, run.narrative.error) == ("在跌", None)
    assert run.narrative.created_at


def test_转不了JSON的内容直接报错_不留残缺文件(store, tmp_path):
    with pytest.raises(TypeError):
        store.save_run(RunRecord(spec={"when": object()}, status="done"))
    assert not any(tmp_path.rglob("*.json")) and not any(tmp_path.rglob("*.tmp"))
