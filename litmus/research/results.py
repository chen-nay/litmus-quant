"""研究结果的数据结构（ARCHITECTURE §4.4）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from litmus.research.returns import Delay


@dataclass(frozen=True)
class Column:
    """结果表的一列。unit 决定页面怎么显示，空串按普通数字。"""

    name: str
    unit: str = ""


@dataclass(frozen=True)
class ListResult:
    """表：每行一个标的。"""

    as_of: date
    total: int  # 满足筛选条件的个数（取前 N 之前）
    pool_size: int  # 算的范围有多少个标的
    head: tuple[str, ...]  # 前几列：代码、名称、（股票还有行业）
    columns: tuple[Column, ...]  # 每个 metric 一列，带单位
    rows: tuple[dict[str, object], ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriggerRecord:
    """个股回看里的一次触发。按持有天数分档的字段，key 是持有天数。"""

    trigger_date: date
    entry_date: date | None
    entry_delay: Delay | None
    status: dict[int, str]  # 完成 / 退市 / 无法成交 / 观察中
    exit_date: dict[int, date | None]
    exit_delay: dict[int, Delay | None]
    returns: dict[int, float | None]  # 未扣成本
    market_returns: dict[int, float | None]  # 同期对照（同一段日历窗口）
    market_excluded: dict[int, int]  # 同期对照里因卖出日停牌拿不到价格而剔除的只数
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class HorizonSummary:
    """持有某个天数的汇总。只算「完成」「退市」的笔数。"""

    n: int
    mean_return: float | None  # 未扣成本
    mean_return_after_cost: float | None  # 扣掉 cost_bps 之后
    mean_market_return: float | None  # 同期对照
    mean_baseline_return: float | None  # 这只股票平时的平均
    win_rate: float | None  # 相对同期对照跑赢的比例
    unfilled: int = 0  # 无法成交
    pending: int = 0  # 观察中


@dataclass(frozen=True)
class HistoryResult:
    """个股回看。"""

    code: str
    name: str | None
    event_label: str
    range: tuple[date, date]  # 实际统计区间（已扣掉预热期）
    benchmark: str
    cost_bps: float
    triggers: tuple[TriggerRecord, ...]
    summary: dict[int, HorizonSummary]
    notes: tuple[str, ...] = field(default_factory=tuple)
