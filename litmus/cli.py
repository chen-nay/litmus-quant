"""命令行入口：启动本地网页服务（ARCHITECTURE §8）。

    uv run python -m litmus serve
    uv run python -m litmus serve --port 8001

同步数据走页面上的按钮（POST /api/data/sync）；开发调试用 `python -m litmus.data`。
"""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from litmus.api import create_app
from litmus.env import load_env

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m litmus", description="litmus 选股条件证伪器")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动本地网页服务")
    serve.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"监听地址（默认 {DEFAULT_HOST}，只有本机能访问）。接口没有登录，不要改成对外开放",
    )
    serve.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）"
    )
    serve.add_argument(
        "--data-dir", default=None, help="数据目录（默认取 LITMUS_DATA_DIR 或仓库下的 data/）"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env()
    if args.data_dir:
        os.environ["LITMUS_DATA_DIR"] = args.data_dir

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 同步时别把每个请求都打出来
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0
