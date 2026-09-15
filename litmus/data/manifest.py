"""manifest.json：本地数据的"户口本"。

记的是**每个数据集同步到哪儿了**，供五件事使用：断点续传、增量同步、页面显示数据截止日、
能力开关（没权限的可选数据要从字段清单里摘掉）、出问题时追溯。

关于"完整日"：磁盘上不会存在半天的数据——一个交易日的必需接口全部拉到才纳入当月面板，
缺任何一个就中止这个月、不落盘（见 storage.py 的不变量）。所以 manifest 只记到月：
行数、交易日数、首末日期，加一个 `complete` 标记区分"整月已走完"和"还在长的当月"。

时间戳用本地时区，带偏移量，人看得懂。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime

import polars as pl

from litmus.data.fields import SW_INDUSTRY_L2_CAPABILITY
from litmus.data.storage import MarketStore, MissingDataError, StorageError, read_json, write_json

#: 没记过的能力为什么不可用：概念板块是还没探测权限；申万二级行业不用探测，是还没同步过
_NOT_RECORDED = {SW_INDUSTRY_L2_CAPABILITY: "还没同步过，同步一次后可用"}

#: 结构变了就加一，老数据目录直接要求重新同步
MANIFEST_VERSION = 1


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class MonthRecord:
    """一个月的落盘记录。`complete=False` 表示这个月还没走完，下次同步要整月重写。"""

    rows: int
    days: int
    first_date: str
    last_date: str
    complete: bool
    synced_at: str


@dataclass(frozen=True)
class TableRecord:
    """不分月的整张表（股票列表、交易日历、财务指标等）。"""

    rows: int
    synced_at: str
    note: str = ""


@dataclass(frozen=True)
class Capability:
    """可选数据的能力探测结果。没探测过一律按不可用处理。"""

    available: bool
    checked_at: str
    reason: str = ""


@dataclass
class Manifest:
    source: str = "tushare"
    updated_at: str = ""
    version: int = MANIFEST_VERSION
    months: dict[str, dict[str, MonthRecord]] = field(default_factory=dict)
    tables: dict[str, TableRecord] = field(default_factory=dict)
    capabilities: dict[str, Capability] = field(default_factory=dict)

    # ── 读写 ────────────────────────────────────────────────────

    @classmethod
    def load(cls, store: MarketStore) -> Manifest:
        """读 manifest；还没同步过就返回一个空的。"""
        try:
            raw = read_json(store.manifest_path)
        except MissingDataError:
            return cls()

        version = raw.get("version")
        if version != MANIFEST_VERSION:
            raise StorageError(
                f"{store.manifest_path} 是第 {version} 版，当前程序用第 {MANIFEST_VERSION} 版；"
                f"删掉 {store.market} 重新同步"
            )
        # 老账本里的 first_sync_done 不再读：历史补没补完从日频月份推导（DataSync.status），
        # 另存一个标记只会和文件对不上。只是少读一个字段，不用升版本，也不用重新同步
        return cls(
            source=raw["source"],
            updated_at=raw["updated_at"],
            months={
                dataset: {m: MonthRecord(**rec) for m, rec in recs.items()}
                for dataset, recs in raw["months"].items()
            },
            tables={name: TableRecord(**rec) for name, rec in raw["tables"].items()},
            capabilities={name: Capability(**rec) for name, rec in raw["capabilities"].items()},
        )

    def save(self, store: MarketStore) -> None:
        self.updated_at = _now()
        write_json(asdict(self), store.manifest_path)

    # ── 按月记账 ────────────────────────────────────────────────

    def record_month(self, dataset: str, month: str, df: pl.DataFrame, *, complete: bool) -> None:
        """记一个月。行数与首末日期从数据本身算，不让调用方传，省得记错。"""
        dates = df.get_column("date")
        self.months.setdefault(dataset, {})[month] = MonthRecord(
            rows=df.height,
            days=dates.n_unique(),
            first_date=str(dates.min()),
            last_date=str(dates.max()),
            complete=complete,
            synced_at=_now(),
        )

    def month(self, dataset: str, month: str) -> MonthRecord | None:
        return self.months.get(dataset, {}).get(month)

    def recorded_months(self, dataset: str) -> tuple[str, ...]:
        return tuple(sorted(self.months.get(dataset, {})))

    def missing_months(
        self, dataset: str, wanted: Iterable[str], store: MarketStore | None = None
    ) -> tuple[str, ...]:
        """`wanted` 里还需要同步的月份——断点续传就靠这个。

        三种情况要拉：没记录过、记了但还没走完（当月）、记了但文件不在了（传了 store 才查）。
        """
        recorded = self.months.get(dataset, {})
        todo = []
        for month in wanted:
            record = recorded.get(month)
            if record is None or not record.complete:
                todo.append(month)
            elif store is not None and not store.has_month(dataset, month):
                todo.append(month)
        return tuple(todo)

    def data_through(self, dataset: str) -> str | None:
        """这个数据集最新一天是哪天——页面上"数据截至 YYYY-MM-DD"用它。"""
        records = self.months.get(dataset)
        if not records:
            return None
        return max(rec.last_date for rec in records.values())

    # ── 整张表 ──────────────────────────────────────────────────

    def record_table(self, name: str, df: pl.DataFrame, note: str = "") -> None:
        self.tables[name] = TableRecord(rows=df.height, synced_at=_now(), note=note)

    # ── 能力探测 ────────────────────────────────────────────────

    def record_capability(self, name: str, available: bool, reason: str = "") -> None:
        self.capabilities[name] = Capability(available=available, checked_at=_now(), reason=reason)

    def is_available(self, name: str) -> bool:
        """没探测过按不可用算：宁可少给一个字段，也不让上层拿到空数据。"""
        capability = self.capabilities.get(name)
        return bool(capability and capability.available)

    def unavailable_reason(self, name: str) -> str:
        capability = self.capabilities.get(name)
        if capability is None:
            return _NOT_RECORDED.get(name, "还没探测过这项数据")
        return capability.reason
