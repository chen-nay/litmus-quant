"""研究记录的 JSON 文件实现：一条记录一个文件（ARCHITECTURE §7）。

    <root>/plans/<plan_id>.json
    <root>/runs/<run_id>.json
    <root>/traces/t<plan_id 或 run_id>.json
    <root>/narratives/n<run_id>.json

- **原子写入**：先写同目录的临时文件再改名，读的人不会读到写了一半的记录；写失败不留残缺文件
- **编号 = 类型字母 + 时间 + 随机后缀**（如 r20260914-15-30-12a1b2c3）：位宽固定，按编号排序就是按时间排序
  （精确到秒），同一秒存两条也不会撞。取记录前先按格式检查编号——编号来自网址，不检查的话 `../`
  能读到目录外的文件
- **过程记录不另编号**：文件名是 `t` + 它记录的那条 plan_id / run_id（`tp2026…` / `tr2026…`），
  一眼看出记的是提问还是运行，又能从 plan_id 直接推出文件名，不用建索引。对外仍按 plan_id / run_id 查
- **小结同样不另编号**：文件名是 `n` + 那次运行的 run_id
- 本地单用户，P0 不做加锁；两次保存总是写不同的文件
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from litmus.store.base import NarrativeRecord, PlanRecord, RunRecord, TraceRecord

_ID = re.compile(r"^[pr]\d{8}-\d{2}-\d{2}-\d{2}[0-9a-f]{6}$")


class JsonStore:
    def __init__(self, root: Path):
        self._root = Path(root)

    def save_plan(self, plan: PlanRecord) -> str:
        plan_id = _new_id("p")
        self._write("plans", plan_id, asdict(replace(plan, plan_id=plan_id, created_at=_now())))
        return plan_id

    def get_plan(self, plan_id: str) -> PlanRecord | None:
        raw = self._read("plans", plan_id, "p")
        return None if raw is None else PlanRecord(**raw)

    def save_run(self, run: RunRecord) -> str:
        run_id = _new_id("r")
        self._write("runs", run_id, asdict(replace(run, run_id=run_id, created_at=_now())))
        return run_id

    def get_run(self, run_id: str) -> RunRecord | None:
        raw = self._read("runs", run_id, "r")
        return None if raw is None else RunRecord(**raw)

    def save_trace(self, trace: TraceRecord) -> str:
        record_id = trace.record_id
        if not _ID.match(record_id):
            raise ValueError(f"过程记录要挂在 plan_id 或 run_id 上，收到 {record_id!r}")
        self._write("traces", f"t{record_id}", asdict(replace(trace, created_at=_now())))
        return record_id

    def get_trace(self, record_id: str) -> TraceRecord | None:
        if not _ID.match(record_id):  # 不限前缀：提问和运行都能挂过程记录
            return None
        path = self._root / "traces" / f"t{record_id}.json"
        if not path.exists():
            return None
        return TraceRecord(**json.loads(path.read_text(encoding="utf-8")))

    def save_narrative(self, narrative: NarrativeRecord) -> str:
        run_id = narrative.run_id
        if not (_ID.match(run_id) and run_id.startswith("r")):
            raise ValueError(f"小结要挂在 run_id 上，收到 {run_id!r}")
        self._write("narratives", f"n{run_id}", asdict(replace(narrative, created_at=_now())))
        return run_id

    def get_narrative(self, run_id: str) -> NarrativeRecord | None:
        if not (_ID.match(run_id) and run_id.startswith("r")):
            return None
        path = self._root / "narratives" / f"n{run_id}.json"
        if not path.exists():
            return None
        return NarrativeRecord(**json.loads(path.read_text(encoding="utf-8")))

    def _write(self, kind: str, record_id: str, payload: dict) -> None:
        text = (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )  # 转不了 JSON 在这里就报错，还没开始写
        path = self._root / kind / f"{record_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _read(self, kind: str, record_id: str, prefix: str) -> dict | None:
        if not _ID.match(record_id) or not record_id.startswith(prefix):
            return None
        path = self._root / kind / f"{record_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def _new_id(prefix: str) -> str:
    return f"{prefix}{datetime.now().strftime('%Y%m%d-%H-%M-%S')}{secrets.token_hex(3)}"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
