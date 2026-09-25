"""研究记录的 JSON 文件实现：一条记录一个文件（ARCHITECTURE §7）。

    <root>/plans/<plan_id>.json      提问：原话、查询条件、候选、过程、引出的运行
    <root>/runs/<run_id>.json        运行：查询条件、结果、过程、卡的小结

- **原子写入**：先写同目录的临时文件再改名，读的人不会读到写了一半的记录；写失败不留残缺文件
- **编号 = 类型字母 + 时间 + 随机后缀**（如 r20260914-15-30-12a1b2c3）：位宽固定，按编号排序就是按时间排序
  （精确到秒），同一秒存两条也不会撞。取记录前先按格式检查编号——编号来自网址，不检查的话 `../`
  能读到目录外的文件
- **过程和小结写在它们属于的那条记录里**：追加过程、挂运行、写小结都是读出来改一处再写回，
  编号按前缀认（`p` 是提问、`r` 是运行）
- 本地单用户，P0 不做加锁；同一次运行的小结由 api 串起来写（一次只写一个）
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from litmus.store.base import Narrative, PlanRecord, RunRecord

_ID = re.compile(r"^[pr]\d{8}-\d{2}-\d{2}-\d{2}[0-9a-f]{6}$")
_KINDS = {"p": "plans", "r": "runs"}


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
        if raw is None:
            return None
        narrative = raw.pop("narrative", None)
        return RunRecord(**raw, narrative=None if narrative is None else Narrative(**narrative))

    def add_steps(self, record_id: str, steps: Sequence[dict[str, object]]) -> None:
        self._update(record_id, lambda raw: {"steps": [*raw["steps"], *steps]})

    def link_run(self, plan_id: str, run_id: str) -> None:
        self._update(
            plan_id,
            lambda raw: {"runs": raw["runs"] if run_id in raw["runs"] else [*raw["runs"], run_id]},
            prefix="p",
        )

    def set_narrative(self, run_id: str, narrative: Narrative) -> None:
        written = asdict(replace(narrative, created_at=_now()))
        self._update(run_id, lambda _: {"narrative": written}, prefix="r")

    def _update(self, record_id: str, change, prefix: str = "") -> None:
        """读出来改几个字段再整份写回。没有这条记录、编号不合法都报错——写进不存在的记录是错，不是无事发生。"""
        kind = _KINDS.get(record_id[:1], "")
        if not _ID.match(record_id) or not kind or not record_id.startswith(prefix):
            raise ValueError(f"不是合法的{'运行' if prefix == 'r' else '记录'}编号：{record_id!r}")
        raw = self._read(kind, record_id, record_id[:1])
        if raw is None:
            raise ValueError(f"没有编号为 {record_id} 的记录")
        self._write(kind, record_id, {**raw, **change(raw)})

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
