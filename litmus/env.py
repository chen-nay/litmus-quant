"""把仓库根目录的 .env 读进环境变量。

由入口（`python -m litmus.data`、`python -m litmus serve`）调用：库本身只读 os.environ，谁调用谁负责把环境准备好。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 仓库根目录下的 .env
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


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
