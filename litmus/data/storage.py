"""本地数据文件的布局、原子写入与按月分片。

只有这一层知道文件在哪：DataSync 写、DataService 读，都通过 MarketStore 拿路径，
不自己拼字符串。布局见 ARCHITECTURE.md §2.5「存储布局」。

两条不变量：

1. **落盘的文件一定是完整的**。先写临时文件再 `os.replace` 改名，读的人要么看到旧版本，
   要么看到新版本，不会读到写了一半的 Parquet。
2. **月文件要么不存在，要么是整月**。一个月的数据全部拉完才写文件；中途退出就当这个月
   没同步过，下次整月重来（一个月约 80 次接口调用、十几秒，比维护半截文件划算）。
   还没走完的当月同样是整月重写，不做追加合并——增量同步每天多花十几秒，换掉一整类
   "合并去重"的 bug。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl

#: 按月分片的文件名，如 2026-09.parquet
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: 环境变量：数据目录，缺省为仓库根目录下的 data/
DATA_DIR_ENV = "LITMUS_DATA_DIR"


class StorageError(RuntimeError):
    """本地数据文件的读写出了问题。"""


class MissingDataError(StorageError):
    """要读的数据本地还没有。"""


def default_root() -> Path:
    """数据目录：优先取 LITMUS_DATA_DIR，否则用仓库根目录下的 data/。"""
    configured = os.getenv(DATA_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    # storage.py → litmus/data → litmus → 仓库根
    return Path(__file__).resolve().parents[2] / "data"


def write_atomic(path: Path, write: Callable[[Path], None]) -> Path:
    """先写同目录下的临时文件，成功后改名到 path。

    写到一半失败就把临时文件删掉，目标文件保持原样——不会留下半截数据。
    临时文件必须和目标同目录，跨文件系统的 rename 不是原子的。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex[:8]}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def write_parquet(df: pl.DataFrame, path: Path) -> Path:
    return write_atomic(path, df.write_parquet)


def write_json(payload: Any, path: Path) -> Path:
    """写 JSON。中文原样保留，带缩进，方便出问题时直接看文件。"""

    def dump(tmp: Path) -> None:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return write_atomic(path, dump)


def read_json(path: Path) -> Any:
    if not path.exists():
        raise MissingDataError(f"{path} 不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def _check_dataset(dataset: str) -> str:
    """数据集名是相对路径片段，可以带子目录（board/concept），但不能跑出数据目录。"""
    if not dataset or dataset.startswith("/") or ".." in Path(dataset).parts:
        raise StorageError(f"数据集名非法：{dataset!r}")
    return dataset


def _check_month(month: str) -> tuple[int, int]:
    if not MONTH_RE.match(month):
        raise StorageError(f"月份要写成 YYYY-MM，收到 {month!r}")
    return int(month[:4]), int(month[5:])


@dataclass(frozen=True)
class MarketStore:
    """`data/market/` 下的行情数据。DataSync 写，DataService 读。"""

    root: Path

    @classmethod
    def from_env(cls) -> MarketStore:
        return cls(default_root())

    @property
    def market(self) -> Path:
        return self.root / "market"

    @property
    def manifest_path(self) -> Path:
        return self.market / "manifest.json"

    def dataset_dir(self, dataset: str) -> Path:
        return self.market / _check_dataset(dataset)

    def month_path(self, dataset: str, month: str) -> Path:
        """按月分片的文件路径，如 market/daily/2026-09.parquet。"""
        _check_month(month)
        return self.dataset_dir(dataset) / f"{month}.parquet"

    def table_path(self, name: str) -> Path:
        """不分片的整张表，如 market/fina_indicator.parquet、market/meta/stock_basic.parquet。"""
        return self.market / f"{_check_dataset(name)}.parquet"

    # ── 按月分片 ────────────────────────────────────────────────

    def write_month(self, dataset: str, month: str, df: pl.DataFrame) -> Path:
        """写一个整月的面板。行必须都属于这个月，否则说明上游拼错了。"""
        year, mon = _check_month(month)
        if df.is_empty():
            raise StorageError(f"{dataset} {month} 没有数据，不写空文件——没有文件就表示没同步过")
        if "date" not in df.columns:
            raise StorageError(f"{dataset} 缺 date 列，无法按月落盘")
        if df.schema["date"] != pl.Date:
            raise StorageError(f"{dataset} 的 date 列应为日期类型，实际是 {df.schema['date']}")

        strays = df.filter((pl.col("date").dt.year() != year) | (pl.col("date").dt.month() != mon))
        if not strays.is_empty():
            sample = strays.select("date").unique().sort("date").head(3).to_series().to_list()
            raise StorageError(f"{strays.height} 行不属于 {month}，如 {sample}")

        order = [c for c in ("date", "code") if c in df.columns]
        return write_parquet(df.sort(order), self.month_path(dataset, month))

    def read_month(self, dataset: str, month: str) -> pl.DataFrame:
        path = self.month_path(dataset, month)
        if not path.exists():
            raise MissingDataError(f"{dataset} 还没有 {month} 的数据")
        return pl.read_parquet(path)

    def has_month(self, dataset: str, month: str) -> bool:
        return self.month_path(dataset, month).exists()

    def months(self, dataset: str) -> tuple[str, ...]:
        """已落盘的月份，从早到晚。"""
        directory = self.dataset_dir(dataset)
        if not directory.is_dir():
            return ()
        return tuple(sorted(p.stem for p in directory.glob("*.parquet") if MONTH_RE.match(p.stem)))

    # ── 整张表 ──────────────────────────────────────────────────

    def write_table(self, name: str, df: pl.DataFrame) -> Path:
        return write_parquet(df, self.table_path(name))

    def read_table(self, name: str) -> pl.DataFrame:
        path = self.table_path(name)
        if not path.exists():
            raise MissingDataError(f"本地还没有 {name}")
        return pl.read_parquet(path)

    def has_table(self, name: str) -> bool:
        return self.table_path(name).exists()
