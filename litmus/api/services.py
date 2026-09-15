"""api 用到的全部依赖：启动时创建一次，路由从 request.app.state 取，测试里换成替身（ARCHITECTURE §1.2 第 4 条）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from fastapi import Request

from litmus.api.sync_job import SyncJob
from litmus.data import DataService, DataStatus, DataSync, MarketStore
from litmus.llm import AnthropicClient, LLMClient, LLMConfig, LLMError
from litmus.signals import EventLibrary, load_events
from litmus.store import JsonStore, Store


@dataclass(frozen=True)
class Services:
    ds: DataService
    store: Store
    #: 事件库只在启动时加载一次（加载时要把全部参数组合校验一遍）
    events: EventLibrary
    sync_job: SyncJob
    #: 本地数据状态，只读 manifest 和文件
    data_status: Callable[[], DataStatus]
    #: 大模型客户端；.env 没配好时为空，/api/plan 返回 failed 并说明缺什么
    llm: LLMClient | None = None

    @classmethod
    def from_env(cls) -> Services:
        """数据目录取 LITMUS_DATA_DIR 或仓库下的 data/：行情在 market/，提问和运行记录在 store/。"""
        market = MarketStore.from_env()
        return cls(
            ds=DataService(market),
            store=JsonStore(market.root / "store"),
            events=load_events(),
            sync_job=SyncJob(partial(DataSync.open, market)),
            data_status=DataSync(None, market).status,
            llm=_llm_from_env(),
        )


def _llm_from_env() -> LLMClient | None:
    try:
        return AnthropicClient(LLMConfig.from_env())
    except LLMError:
        return None


def services_of(request: Request) -> Services:
    return request.app.state.services
