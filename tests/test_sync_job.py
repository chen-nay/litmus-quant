"""后台同步任务的测试：用假同步器，不连 Tushare、不写数据。"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import date

from litmus.api.sync_job import DONE, FAILED, IDLE, RUNNING, STOPPED, STOPPING, SyncJob
from litmus.data import SYNC_STEPS, MonthResult, TushareError


class FakeSync:
    """记录跑了哪些步骤；日频按月回调进度，gate 不放行就停在每个月之前。"""

    def __init__(
        self, months: int = 3, fail_at: str | None = None, gate: threading.Event | None = None
    ):
        self.months = months
        self.fail_at = fail_at
        self.gate = gate
        self.calls: list[tuple[str, str, str]] = []

    def sync_all(self, start, end, manifest=None, steps=None, on_month=None):
        (step,) = steps
        self.calls.append((start, end, step))
        if step == self.fail_at:
            raise RuntimeError("网络炸了")
        if step == "daily":
            for i in range(1, self.months + 1):
                if self.gate is not None:
                    self.gate.wait(timeout=5)
                on_month(MonthResult(f"2026-0{i}", 100, 20, (), True), i, self.months)
        return {}


def job_for(fake: FakeSync) -> SyncJob:
    @contextmanager
    def opener():
        yield fake

    return SyncJob(opener, today=lambda: date(2026, 9, 14))


def wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "等太久了"
        time.sleep(0.01)


def test_没开始时是空闲():
    assert job_for(FakeSync()).snapshot()["state"] == IDLE


def test_按固定顺序跑完全部步骤_记下日频进度():
    fake = FakeSync()
    job = job_for(fake)
    assert job.start()
    assert job.wait(5)
    progress = job.snapshot()
    assert progress["state"] == DONE and progress["error"] is None
    assert progress["steps_done"] == list(SYNC_STEPS)
    assert (progress["daily_month"], progress["daily_done"], progress["daily_total"]) == (
        "2026-03",
        3,
        3,
    )
    assert progress["started_at"] and progress["finished_at"]
    assert [step for _, _, step in fake.calls] == list(SYNC_STEPS)
    assert fake.calls[0][:2] == ("20160101", "20260914")  # 从 2016 年到今天，缺什么补什么


def test_同步中再点不会再开一个():
    gate = threading.Event()
    job = job_for(FakeSync(gate=gate))
    assert job.start()
    wait_until(lambda: job.snapshot()["step"] == "daily")
    assert job.start() is False
    assert job.snapshot()["state"] == RUNNING
    gate.set()
    assert job.wait(5) and job.snapshot()["state"] == DONE


def test_停止_在月份之间停下_已完成的不受影响():
    gate = threading.Event()
    job = job_for(FakeSync(gate=gate))
    job.start()
    wait_until(lambda: job.snapshot()["step"] == "daily")
    assert job.stop()
    assert job.snapshot()["state"] == STOPPING
    gate.set()
    assert job.wait(5)
    progress = job.snapshot()
    assert progress["state"] == STOPPED
    assert progress["daily_done"] == 1  # 第一个月落盘后检查到停止
    assert "daily" not in progress["steps_done"]


def test_没在同步时停止不做任何事():
    job = job_for(FakeSync())
    assert job.stop() is False


def test_出错就停下_记下原因_再点可以接着同步():
    job = job_for(FakeSync(fail_at="finance"))
    job.start()
    assert job.wait(5)
    progress = job.snapshot()
    assert progress["state"] == FAILED
    assert progress["error"] == "RuntimeError: 网络炸了"
    assert progress["steps_done"] == list(SYNC_STEPS[: SYNC_STEPS.index("finance")])
    assert job.start()  # 出错之后可以再开
    assert job.wait(5)


def test_打不开同步器_比如没配token():
    @contextmanager
    def opener():
        raise TushareError("缺少 TUSHARE_TOKEN，请在 .env 中配置")
        yield  # pragma: no cover

    job = SyncJob(opener)
    job.start()
    assert job.wait(5)
    assert job.snapshot()["error"] == "TushareError: 缺少 TUSHARE_TOKEN，请在 .env 中配置"
