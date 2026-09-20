"""收益计算的纯函数：买卖日期、顺延、退市、涨跌（ARCHITECTURE §4.3）。

口径：

    买入日 = 触发日的下一个交易日；开盘涨停或停牌 → 顺延到第一个「有行情且开盘没涨停」的交易日
    卖出日 = 买入日之后第 N 个交易日（按交易日历数，从买入日起算）；收盘跌停或停牌 → 顺延到第一个「有行情且没跌停」的交易日
    涨跌   = 卖出日收盘价 / 买入日开盘价 - 1，都用后复权价（含分红送转）

每一笔的结局（2026-09-14 定）：

    完成     正常买卖。计入平均
    退市     持有期内退市、之后再也卖不出去：按退市前最后一个交易日的收盘价结算。计入平均
    无法成交 买入或卖出顺延超过 20 个交易日（长期停牌）。不计入平均
    观察中   买入日或顺延后的卖出日落在本地数据末日之后，结果还没出来。不计入平均

缺涨跌停价的日子（2016~2019 年 886 行）判断不了涨跌停，按可以成交处理，备注里写明。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import polars as pl

#: 顺延最多等多少个交易日，再多就算无法成交
MAX_DELAY = 20

DONE, DELISTED, UNFILLED, PENDING = "完成", "退市", "无法成交", "观察中"
#: 计入平均的结局
COUNTED = frozenset({DONE, DELISTED})

MISSING_LIMIT_NOTE = "涨跌停价缺失，按可以成交处理"
DELISTED_NOTE = "持有期内退市，按退市前最后一个交易日的收盘价结算"

#: 顺延原因的先后顺序
_REASON_ORDER = ("涨停", "跌停", "停牌")


@dataclass(frozen=True)
class Delay:
    days: int
    reason: str  # 涨停 / 跌停 / 停牌，几种都有时用「、」连起来


@dataclass(frozen=True)
class Trade:
    status: str
    entry_date: date | None = None
    entry_delay: Delay | None = None
    exit_date: date | None = None
    exit_delay: Delay | None = None
    ret: float | None = None  # 未扣成本
    notes: tuple[str, ...] = ()


class Tape:
    """一只股票铺在交易日历上的行情，用来一笔一笔地模拟买卖。

    calendar：本地交易日历，最后一天就是本地数据末日。
    rows：(date, open, close, open_limit_up, is_limit_down)，后复权价；停牌日没有行。
    delist_date：退市日，没退市为 None。
    """

    def __init__(
        self, calendar: Sequence[date], rows: pl.DataFrame, delist_date: date | None = None
    ):
        self.calendar = list(calendar)
        self._index = {day: i for i, day in enumerate(self.calendar)}
        size = len(self.calendar)
        self._has = [False] * size
        self._open: list[float | None] = [None] * size
        self._close: list[float | None] = [None] * size
        self._open_up: list[bool | None] = [None] * size
        self._down: list[bool | None] = [None] * size
        columns = ("date", "open", "close", "open_limit_up", "is_limit_down")
        for day, open_, close, open_up, down in rows.select(columns).iter_rows():
            i = self._index.get(day)
            if i is None:
                continue
            self._has[i] = True
            self._open[i], self._close[i] = open_, close
            self._open_up[i], self._down[i] = open_up, down
        traded = [i for i, has in enumerate(self._has) if has]
        self._last_row = traded[-1] if traded else None
        self.delisted = delist_date is not None
        # 每个位置起第一个能卖出的日子。退市判断要看「之后还卖不卖得出去」，逐个往后找的话，
        # 把每个交易日都当起点算「平时的平均」会变成平方级
        self._next_sellable: list[int | None] = [None] * (size + 1)
        for i in range(size - 1, -1, -1):
            self._next_sellable[i] = i if self._sellable(i) else self._next_sellable[i + 1]

    def trade_days(self) -> list[date]:
        """这只股票有行情的交易日。"""
        return [day for day, has in zip(self.calendar, self._has, strict=True) if has]

    def trade(self, trigger: date, horizon: int) -> Trade:
        """触发日 trigger 之后持有 horizon 个交易日的这一笔。"""
        start = self._index.get(trigger)
        if start is None:
            raise ValueError(f"{trigger} 不在交易日历里")
        end = len(self.calendar) - 1
        want_buy = start + 1
        if self.delisted and (self._last_row is None or self._last_row < want_buy):
            return Trade(UNFILLED, notes=("触发之后已经退市，买不进",))

        buy, reason = self._search(want_buy, self._open_up, "涨停")
        if buy is None:
            if want_buy + MAX_DELAY <= end:
                return Trade(UNFILLED, notes=(f"买入顺延超过 {MAX_DELAY} 个交易日（{reason}）",))
            return Trade(PENDING)
        notes = [MISSING_LIMIT_NOTE] if self._open_up[buy] is None else []
        entry = self.calendar[buy]
        entry_delay = Delay(buy - want_buy, reason) if buy > want_buy else None
        want_sell = buy + horizon

        if (
            self.delisted
            and self._last_row is not None
            and not self._can_sell_between(want_sell, self._last_row)
        ):
            return Trade(
                DELISTED,
                entry,
                entry_delay,
                self.calendar[self._last_row],
                None,
                self._return(buy, self._last_row),
                (*notes, DELISTED_NOTE),
            )

        sell, reason = self._search(want_sell, self._down, "跌停")
        if sell is None:
            if want_sell + MAX_DELAY <= end:
                note = f"卖出顺延超过 {MAX_DELAY} 个交易日（{reason}）"
                return Trade(UNFILLED, entry, entry_delay, notes=(*notes, note))
            return Trade(PENDING, entry, entry_delay, notes=tuple(notes))
        if self._down[sell] is None and MISSING_LIMIT_NOTE not in notes:
            notes.append(MISSING_LIMIT_NOTE)
        exit_delay = Delay(sell - want_sell, reason) if sell > want_sell else None
        return Trade(
            DONE,
            entry,
            entry_delay,
            self.calendar[sell],
            exit_delay,
            self._return(buy, sell),
            tuple(notes),
        )

    def _sellable(self, i: int) -> bool:
        return self._has[i] and self._down[i] is not True

    def _can_sell_between(self, first: int, last: int) -> bool:
        if first >= len(self.calendar):
            return False
        found = self._next_sellable[first]
        return found is not None and found <= last

    def _search(self, first: int, blocked: list[bool | None], flag: str) -> tuple[int | None, str]:
        """从 first 起最多等 MAX_DELAY 个交易日，找第一个有行情、且 blocked 不为真的日子。返回 (位置, 顺延原因)。"""
        reasons: set[str] = set()
        for i in range(first, min(first + MAX_DELAY, len(self.calendar) - 1) + 1):
            if not self._has[i]:
                reasons.add("停牌")
            elif blocked[i] is True:
                reasons.add(flag)
            else:
                return i, _join(reasons)
        return None, _join(reasons)

    def _return(self, buy: int, sell: int) -> float | None:
        open_, close = self._open[buy], self._close[sell]
        if open_ is None or close is None or open_ == 0:
            return None
        value = close / open_ - 1
        return value if math.isfinite(value) else None


def _join(reasons: set[str]) -> str:
    return "、".join(reason for reason in _REASON_ORDER if reason in reasons)


def pool_average(
    windows: pl.DataFrame,
    pool: pl.DataFrame,
    prices: pl.DataFrame,
    delisted: pl.DataFrame,
) -> pl.DataFrame:
    """同期市场平均：每个窗口里，买入日股票池内全部股票从买入日开盘到卖出日收盘的等权平均涨跌。

    windows：(window, buy_date, sell_date)；pool：按日股票池 (date, code)；
    prices：(date, code, open, close)，后复权；delisted：退市股 (code, last_date, last_close)。
    返回 (window, market_return, used, excluded)。

    - 对照股票不顺延：两边比较的必须是同一段时间
    - 持有期内退市（最后交易日早于卖出日）的按最后收盘价算进去，和被回看的股票同一口径——
      剔掉的话，市场平均会被系统性抬高（2026-09-14 实测持有 60 天最多高估约 0.1 个百分点）
    - 其余卖出日拿不到价格的（停牌）剔除，记下只数
    """
    entries = windows.join(pool.rename({"date": "buy_date"}), on="buy_date").join(
        prices.select(pl.col("date").alias("buy_date"), "code", "open"),
        on=["buy_date", "code"],
        how="left",
    )
    exits = entries.join(
        prices.select(pl.col("date").alias("sell_date"), "code", "close"),
        on=["sell_date", "code"],
        how="left",
    ).join(delisted, on="code", how="left")
    final_close = (
        pl.when(pl.col("close").is_not_null())
        .then(pl.col("close"))
        .when(pl.col("last_date") < pl.col("sell_date"))
        .then(pl.col("last_close"))
    )
    ret = final_close / pl.col("open") - 1
    result = exits.with_columns(pl.when(ret.is_finite()).then(ret).alias("_ret"))
    return (
        result.group_by("window")
        .agg(
            pl.col("_ret").mean().alias("market_return"),
            pl.col("_ret").count().alias("used"),
            pl.col("_ret").null_count().alias("excluded"),
        )
        .sort("window")
    )
