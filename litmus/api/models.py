"""HTTP 请求/响应模型（ARCHITECTURE §6）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Issue(BaseModel):
    """请求里要改的一处。"""

    #: 出问题的栏目，如 "filter.expr"、"event.params.ma"；说不上是哪一栏时为空
    path: str | None = None
    message: str
    #: 可选范围，如事件参数的取值、申万行业名
    allowed: str | None = None
    #: 表达式里第几个字符出的错，从 0 数
    position: int | None = None


class RunResponse(BaseModel):
    status: Literal["done", "needs_revision", "data_not_ready", "failed"]
    #: needs_revision：要改的地方，逐条列出
    issues: list[Issue] = Field(default_factory=list)
    #: done / failed：运行记录编号，GET /api/run/{run_id} 取回
    run_id: str | None = None
    #: done：股票表 / 板块表 / 个股回看，按 shape 区分
    result: dict[str, Any] | None = None
    #: data_not_ready / failed：给人看的一句话
    message: str | None = None
    #: data_not_ready：本地数据状态与同步进度，和 GET /api/data/status 一样
    data: dict[str, Any] | None = None
