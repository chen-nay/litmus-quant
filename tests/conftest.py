"""把 .env 读进环境变量，好让需要 token 的联调测试能自己决定跳不跳。"""

from __future__ import annotations

import os
import pathlib

_ENV_FILE = pathlib.Path(__file__).resolve().parents[1] / ".env"


def _load_dotenv() -> None:
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()
