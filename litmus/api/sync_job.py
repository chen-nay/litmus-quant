"""后台同步任务（ARCHITECTURE §2.5、§6）。

- 服务进程里开一个后台线程跑同步，**同一时间只有一个**：进程内靠这里的状态，跨进程靠 DataSync.open 的锁文件
- 按 SYNC_STEPS 一步一步跑。**每一步之间、日频每个月之间检查「停止」**：停下时已经落盘的不受影响，
  下次同步从断点接着来。正在跑的那一步（财务、概念板块各要两三分钟）会先跑完再停
- **出错就停下**，原因记在进度里；用户再点一次同步就从断点继续，不自动重试（接口层面的重试 loader 已经做了）。
  **概念板块例外**：它每次整张重拉、最容易撞限流（2026-09-14 实测一次增量同步重试 57 次），
  出错时接着跑后面的步骤，最后标为失败并写明原因——不能让它挡住新行情，日频也不依赖它
- 进度只在内存里：服务重启就没了，但已经同步完的月份记在 manifest 里，数据不会丢
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from litmus.data import HISTORY_START, SYNC_STEPS, DataSync, MonthResult

logger = logging.getLogger(__name__)

#: 出错也接着跑后面步骤的
CONTINUE_ON_ERROR = frozenset({"concept"})

_STEP_LABELS = {
    "meta": "基础数据",
    "industry": "申万行业",
    "concept": "概念板块",
    "index": "指数",
    "finance": "财务",
    "daily": "股票日频",
}

IDLE, RUNNING, STOPPING, STOPPED, FAILED, DONE = (
    "idle",
    "running",
    "stopping",
    "stopped",
    "failed",
    "done",
)


class _Stopped(Exception):
    """用户要求停止。"""


@dataclass
class SyncProgress:
    state: str = IDLE  # idle / running / stopping / stopped / failed / done
    step: str | None = None  # 正在跑的步骤
    steps_done: list[str] = field(default_factory=list)
    steps_failed: list[str] = field(default_factory=list)  # 出错但没拦着后面步骤的
    daily_month: str | None = None  # 日频最近同步完的月份
    daily_done: int = 0
    daily_total: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


class SyncJob:
    """open_sync：每次同步时调用，返回一个能 with 的同步器（正式环境是 DataSync.open）。"""

    def __init__(
        self,
        open_sync: Callable[[], AbstractContextManager[DataSync]],
        today: Callable[[], date] = date.today,
    ):
        self._open_sync = open_sync
        self._today = today
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._progress = SyncProgress()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """开始同步。已经在同步就不再开，返回 False。"""
        with self._lock:
            if self._progress.state in (RUNNING, STOPPING):
                return False
            self._stop.clear()
            self._progress = SyncProgress(state=RUNNING, started_at=_now())
            self._thread = threading.Thread(target=self._run, name="litmus-sync", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> bool:
        """要求停止：在下一个步骤或下一个月份之前停下。没在同步返回 False。"""
        with self._lock:
            if self._progress.state != RUNNING:
                return False
            self._stop.set()
            self._progress.state = STOPPING
            return True

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return asdict(self._progress)  # asdict 连里面的列表一起复制，拿到的是快照

    def wait(self, timeout: float | None = None) -> bool:
        """等后台线程结束（测试和服务关闭时用）。返回线程是否已经结束。"""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        end = self._today().strftime("%Y%m%d")
        state, errors = DONE, []
        try:
            with self._open_sync() as sync:
                for step in SYNC_STEPS:
                    self._check_stop()
                    with self._lock:
                        self._progress.step = step
                    try:
                        sync.sync_all(HISTORY_START, end, steps=[step], on_month=self._on_month)
                    except _Stopped:
                        raise
                    except Exception as exc:
                        if step not in CONTINUE_ON_ERROR:
                            raise
                        logger.exception("同步 %s 出错，接着跑后面的步骤", step)
                        errors.append(_describe(exc, step))
                        with self._lock:
                            self._progress.steps_failed.append(step)
                        continue
                    with self._lock:
                        self._progress.steps_done.append(step)
        except _Stopped:
            state = STOPPED
        except Exception as exc:  # noqa: BLE001 —— 后台线程里的错误要记进进度给页面看，不能只打日志
            logger.exception("同步中断")
            state = FAILED
            errors.append(_describe(exc, self._progress.step))
        if errors and state == DONE:
            state = FAILED
        with self._lock:
            self._progress.state = state
            self._progress.step = None
            self._progress.error = "；".join(errors) or None
            self._progress.finished_at = _now()

    def _on_month(self, result: MonthResult, index: int, total: int) -> None:
        with self._lock:
            self._progress.daily_month = result.month
            self._progress.daily_done = index
            self._progress.daily_total = total
        if index < total:  # 这个月已经落盘、记账，停在这里是安全的；最后一个月之后已经没有要停的了
            self._check_stop()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise _Stopped


def _describe(exc: Exception, step: str | None) -> str:
    """如「概念板块：TushareError: 您请求速度过快」。还没开始跑步骤（比如连不上）就不带步骤名。"""
    reason = f"{type(exc).__name__}: {exc}"
    return reason if step is None else f"{_STEP_LABELS.get(step, step)}：{reason}"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
