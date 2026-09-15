"""FastAPI 应用。依赖启动时创建一次，经 Services 交给各个路由（ARCHITECTURE §1.2 第 4 条）。"""

from __future__ import annotations

from fastapi import FastAPI

from litmus import __version__
from litmus.api.routes import catalog, data, plan, runs, stocks
from litmus.api.services import Services


def create_app(services: Services | None = None) -> FastAPI:
    """services 不传就按环境变量创建（LITMUS_DATA_DIR、TUSHARE_TOKEN）；测试里传替身。"""
    app = FastAPI(title="litmus", version=__version__)
    app.state.services = services or Services.from_env()
    for module in (plan, runs, catalog, data, stocks):
        app.include_router(module.router)
    return app
