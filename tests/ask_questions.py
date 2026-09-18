"""把 QUESTIONS.md 里的真实提问逐条问一遍真实大模型，记下每条落到哪个形态、和清单上写的对不对得上。

不是 pytest 用例（大模型的回答每次不完全一样，「对不对」要人看），是一个跑完出报告的脚本：

    uv run python tests/ask_questions.py                 # 全部，4 条同时问
    uv run python tests/ask_questions.py A101 B104 G1    # 只问编号以这些开头的
    uv run python tests/ask_questions.py --workers 1

- 调真实大模型、读本地数据；表和统计只到确认卡，卡提问时就算完，要小结的再取一次小结。不同步，不连 Tushare
- 提问、运行记录写临时目录，不动 data/store
- 每条的完整回答写进 data/questions/<时间>.jsonl（本地，不提交），终端打一张对照表
- 形态对照：清单写「卡」「卡＋话」的，回答要是算完的卡（done）；「表」「统计」要出对应的确认卡；
  「拒」可以是改写建议，也可以是反问（F104 本来就该先澄清）。清单写「卡＋话」的，还要核对 narrate
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from litmus.api import Services, SyncJob, create_app
from litmus.data import DataService, DataSync, MarketStore
from litmus.env import load_env
from litmus.llm import AnthropicClient, LLMConfig
from litmus.signals import load_events
from litmus.store import JsonStore

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "QUESTIONS.md"
OUT_DIR = ROOT / "data" / "questions"

_ROW = re.compile(r"^\| ([A-G]\d{3}) \| (.+?) \| (.+?) \| (.+?) \|")


@dataclass(frozen=True)
class Question:
    id: str
    query: str
    want: str  # 想知道什么 / 期望的反应
    shape: str  # 清单上的形态


@dataclass
class Answer:
    id: str
    query: str
    shape: str
    got: str  # 落到了哪个形态
    match: bool
    status: str
    seconds: float
    attempts: int | None
    narrate: bool | None
    detail: dict


def load_questions(prefixes: list[str]) -> list[Question]:
    """清单里每一行表格。一行写了几种说法的（G104「涨幅前 10 / 涨得最多的 10 只 / …」）拆开各问一次。"""
    found = []
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        qid, query, want, shape = (part.strip() for part in match.groups())
        if prefixes and not any(qid.startswith(prefix) for prefix in prefixes):
            continue
        variants = [part.strip() for part in query.split(" / ")]
        for index, text in enumerate(variants):
            suffix = f"-{index + 1}" if len(variants) > 1 else ""
            found.append(Question(qid + suffix, text, want, shape))
    return found


def landed(body: dict) -> str:
    """回答落到了哪个形态，用清单上的叫法。"""
    status = body["status"]
    if status == "done":
        return "卡" if (body.get("result") or {}).get("kind") == "card" else "?"
    if status == "ok":
        return {"table": "表", "event_study": "统计", "card": "卡"}.get(
            (body.get("spec") or {}).get("output", {}).get("kind"), "?"
        )
    if status in ("unsupported", "not_an_event"):
        return "拒"
    if status == "needs_clarification":
        return "候选" if body.get("choices") else "澄清"
    return "失败"


def matches(expected: str, got: str) -> bool:
    if expected.startswith("卡"):
        return got == "卡"
    if expected == "拒":
        return got in ("拒", "澄清")
    return got == expected


def ask(client: TestClient, store: JsonStore, question: Question) -> Answer:
    started = time.perf_counter()
    body = client.post("/api/plan", json={"query": question.query}).json()
    seconds = time.perf_counter() - started
    record = store.get_plan(body["plan_id"]) if body.get("plan_id") else None
    narrate = None
    if body.get("run_id"):
        narrate = client.get(f"/api/run/{body['run_id']}").json()["spec"].get("narrate")
        if body["result"].get("narrate"):  # 卡先出，小结再单独取，和页面一样
            narrative = client.post(f"/api/run/{body['run_id']}/narrative").json()
            body["result"]["narrative"] = narrative["text"]
    got = landed(body)
    return Answer(
        id=question.id,
        query=question.query,
        shape=question.shape,
        got=got,
        match=matches(question.shape, got),
        status=body["status"],
        seconds=round(seconds, 1),
        attempts=record.detail.get("attempts") if record else None,
        narrate=narrate,
        detail=body,
    )


def summary_line(answer: Answer) -> str:
    """终端上一行：编号、对不对、清单形态 → 实际形态、耗时、一句要点。"""
    body = answer.detail
    mark = "✅" if answer.match else "❌"
    if answer.shape.startswith("卡＋话") and answer.got == "卡" and not answer.narrate:
        mark = "⚠️"  # 形态对了，但该写话的没开
    point = body.get("message") or ""
    if body.get("questions"):
        point = "追问：" + "；".join(q["question"] for q in body["questions"])
    elif body.get("spec") and answer.got in ("表", "统计"):
        point = body.get("summary", "")
    elif body.get("result"):
        rows = body["result"]["items"][0]["rows"] if body["result"].get("items") else []
        point = "；".join(f"{row['name']} {row['text']}" for row in rows[:4])
        if narrative := body["result"].get("narrative"):
            point += f"\n        话：{narrative}"
    return (
        f"{mark} {answer.id:<7} {answer.shape:<5} → {answer.got:<3} {answer.seconds:>5.0f}s"
        f" ×{answer.attempts or '-'}  {answer.query}\n        {point[:300]}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prefixes", nargs="*", help="只问编号以这些开头的，如 A1 B104")
    parser.add_argument("--workers", type=int, default=4, help="同时问几条")
    args = parser.parse_args()

    load_env()
    questions = load_questions(args.prefixes)
    market = MarketStore.from_env()
    store = JsonStore(Path(tempfile.mkdtemp()))
    services = Services(
        ds=DataService(market),
        store=store,
        events=load_events(),
        sync_job=SyncJob(lambda: None),
        data_status=DataSync(None, market).status,
        llm=AnthropicClient(LLMConfig.from_env()),
    )
    app = create_app(services)
    local = threading.local()

    def one(question: Question) -> Answer:
        # TestClient 不保证能跨线程共用，每个线程一个
        if not hasattr(local, "client"):
            local.client = TestClient(app)
        return ask(local.client, store, question)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{datetime.now():%Y%m%d-%H%M}.jsonl"
    print(f"{len(questions)} 条，{args.workers} 条同时问，完整回答写进 {out}")
    started = time.perf_counter()
    with ThreadPoolExecutor(args.workers) as pool, out.open("w", encoding="utf-8") as file:
        answers = []
        for answer in pool.map(one, questions):
            answers.append(answer)
            file.write(json.dumps(asdict(answer), ensure_ascii=False) + "\n")
            file.flush()
            print(summary_line(answer), flush=True)

    right = sum(answer.match for answer in answers)
    by_got: dict[str, int] = {}
    for answer in answers:
        by_got[answer.got] = by_got.get(answer.got, 0) + 1
    minutes = (time.perf_counter() - started) / 60
    print(f"\n形态对得上 {right}/{len(answers)}；落到：{by_got}；用时 {minutes:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
