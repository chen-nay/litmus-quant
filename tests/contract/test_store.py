"""研究记录存储的契约测试（ARCHITECTURE §7）。写到临时目录，不需要任何数据。

以后换 SQLite 实现，同一套测试照样要过。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from litmus.store import JsonStore, PlanRecord, RunRecord


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


def test_编号格式_时间部分按保存先后递增_同一秒也不撞(store):
    ids = [store.save_run(RUN) for _ in range(3)]
    assert all(re.fullmatch(r"r\d{14}[0-9a-f]{6}", run_id) for run_id in ids)
    assert len(set(ids)) == 3
    stamps = [run_id[1:15] for run_id in ids]  # 精确到秒；同一秒内的几条之间不保证先后
    assert stamps == sorted(stamps)
    assert re.fullmatch(
        r"p\d{14}[0-9a-f]{6}", store.save_plan(PlanRecord(query="x", status="failed"))
    )


def test_查不到或编号不合法返回空_不会读到目录外面(store, tmp_path):
    (tmp_path / "secret.json").write_text('{"spec": {}, "status": "done"}', encoding="utf-8")
    assert store.get_run("r20260914153012a1b2c3") is None
    assert store.get_run("../secret") is None
    assert store.get_run("p20260914153012a1b2c3") is None  # 提问记录的编号拿去当运行记录取
    assert store.get_plan("") is None


def test_一条记录一个文件_不留临时文件(store, tmp_path):
    run_id = store.save_run(RUN)
    plan_id = store.save_plan(PlanRecord(query="x", status="ok"))
    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert files == [f"plans/{plan_id}.json", f"runs/{run_id}.json"]


def test_转不了JSON的内容直接报错_不留残缺文件(store, tmp_path):
    with pytest.raises(TypeError):
        store.save_run(RunRecord(spec={"when": object()}, status="done"))
    assert not any(tmp_path.rglob("*.json")) and not any(tmp_path.rglob("*.tmp"))
