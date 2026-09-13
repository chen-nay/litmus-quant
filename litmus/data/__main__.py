"""命令行入口：把 Tushare 的数据同步到本地。

    uv run python -m litmus.data --start 20240101
    uv run python -m litmus.data --start 20160101 --only daily

只做三件事：读 `.env`、解析参数、调 `DataSync`。同步的编排逻辑全在 sync.py，
这里不放任何业务判断——将来网页上的「同步历史数据」按钮走的是同一个 `sync_all()`。

`.env` 的加载放在入口而不是库里：库只读 `os.environ`，谁调用谁负责把环境准备好。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

from litmus.data.loaders.tushare import MAX_CONCURRENCY, TushareClient, TushareConfig
from litmus.data.manifest import Manifest
from litmus.data.storage import MarketStore
from litmus.data.sync import SYNC_STEPS, DataSync, MonthResult

#: 仓库根目录下的 .env
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    """把 .env 读进 os.environ。已经设过的环境变量优先，不覆盖。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m litmus.data",
        description="把 Tushare 数据同步到本地（Parquet + manifest）",
    )
    parser.add_argument("--start", default="20160101", help="起始日期 YYYYMMDD（默认 20160101）")
    parser.add_argument("--end", default=None, help="结束日期 YYYYMMDD（默认今天）")
    parser.add_argument(
        "--only",
        nargs="+",
        choices=SYNC_STEPS,
        metavar="STEP",
        help=f"只跑其中几步，可选：{' '.join(SYNC_STEPS)}（默认全跑）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_CONCURRENCY,
        help=f"线程数上限（默认 {MAX_CONCURRENCY}）；实际并发由自适应限速自己找",
    )
    parser.add_argument(
        "--data-dir", default=None, help="数据目录（默认取 LITMUS_DATA_DIR 或仓库下的 data/）"
    )
    return parser


def report_month(result: MonthResult, index: int, total: int) -> None:
    flag = "整月" if result.complete else f"未走完（跳过 {len(result.skipped_days)} 天）"
    print(
        f"  [{index}/{total}] {result.month}  {result.rows} 行 / {result.days} 天  {flag}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env()
    if args.data_dir:
        os.environ["LITMUS_DATA_DIR"] = args.data_dir

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 别把每个请求都打出来

    end = args.end or date.today().strftime("%Y%m%d")
    store = MarketStore.from_env()
    print(f"数据目录：{store.root}")
    print(f"区间：{args.start} ~ {end}，步骤：{' '.join(args.only or SYNC_STEPS)}\n", flush=True)

    try:
        with TushareClient(TushareConfig.from_env()) as client:
            sync = DataSync(client, store, workers=args.workers)
            summary = sync.sync_all(
                args.start, end, Manifest.load(store), steps=args.only, on_month=report_month
            )
    except Exception as exc:  # noqa: BLE001 —— 命令行要给人看懂的错，不是 traceback
        print(f"\n同步中断：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("已经落盘的部分不受影响，重跑会从断点继续。", file=sys.stderr)
        return 1

    print("\n=== 完成 ===")
    for step, detail in summary.items():
        print(f"  {step}: {detail}")
    manifest = Manifest.load(store)
    print(f"数据截至：{manifest.data_through('daily')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
