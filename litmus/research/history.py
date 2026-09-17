"""个股回看：找出事件的每一次触发 → 算之后 N 天涨跌 → 和同期对照、这只股票平时比（ARCHITECTURE §4.3）。

- 触发日：事件表达式包一层「由不满足变为满足」（expr.onset），实际统计起点扣掉预热期
- 每一笔的买卖、顺延、退市、观察中见 returns.py
- 同期对照（2026-09-14 定）：全A等权用**买入日**的股票池（剔除 ST、停牌、次新），对照股票不顺延，
  持有期内退市的按最后价格算进去；或者换成沪深300 / 中证500 指数
- 这只股票平时的平均：统计区间里它每个有行情的交易日都当作起点，用同样的规则算，不排除触发日、不扣成本
- 只有「完成」「退市」计入平均；P0 不做显著性检验、不给「有效」结论（§11）
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import polars as pl

from litmus.data import STOCK, DataService, MissingDataError
from litmus.expr import Evaluation, evaluate, onset, parse
from litmus.research.results import HistoryResult, HorizonSummary, TriggerRecord
from litmus.research.returns import COUNTED, PENDING, UNFILLED, Tape, Trade, pool_average
from litmus.spec import DEFAULTS, StockHistorySpec

#: 触发次数少于这个数，提示样本偏少
FEW_TRIGGERS = 20

#: 全A等权对照的股票池剔除项，和股票表的默认值一致
BENCHMARK_EXCLUDE: tuple[str, ...] = DEFAULTS["exclude"]  # type: ignore[assignment]

_TAPE_FIELDS = ["open", "close", "open_limit_up", "is_limit_down"]


def run_stock_history(spec: StockHistorySpec, ds: DataService) -> HistoryResult:
    coverage_start, latest, start, end, rows, evaluation = _scan(spec, ds, _TAPE_FIELDS)
    code = spec.target.code or ""
    calendar = ds.get_trading_calendar(coverage_start, latest)
    info = ds.stock_info([code], end).row(0, named=True)
    tape = Tape(calendar, rows, info["delist_date"])
    first = evaluation.first_date or start
    trigger_days = evaluation.values.filter(pl.col("value")).get_column("date").to_list()

    trades = {day: {h: tape.trade(day, h) for h in spec.horizons} for day in trigger_days}
    market = _market_returns(spec.benchmark, trades, coverage_start, latest, ds)
    records = tuple(_record(day, trades[day], market) for day in trigger_days)

    samples = [day for day in tape.trade_days() if first <= day <= end]
    summary = {
        h: _summarize(h, records, [tape.trade(day, h) for day in samples], spec.cost_bps)
        for h in spec.horizons
    }
    return HistoryResult(
        code=code,
        name=info["name"],
        event_label=spec.event.label,
        range=(first, end),
        benchmark=spec.benchmark,
        cost_bps=spec.cost_bps,
        triggers=records,
        summary=summary,
        notes=_notes(len(records), summary, start, end, latest),
    )


def statistics_range(spec: StockHistorySpec, ds: DataService) -> tuple[date, date]:
    """实际统计的区间：回看区间裁到本地数据里，再扣掉事件的预热期。和结果里的 range 是同一段代码算的，
    确认卡上显示（§3.5）。"""
    _, _, start, end, _, evaluation = _scan(spec, ds, ["close"])
    return evaluation.first_date or start, end


def _scan(
    spec: StockHistorySpec, ds: DataService, fields: list[str]
) -> tuple[date, date, date, date, pl.DataFrame, Evaluation]:
    """裁区间、取这只股票的行情、求事件表达式。返回 (本地数据起点, 最新一天, 起点, 终点, 行情, 求值结果)。"""
    code = spec.target.code
    if not code:
        raise ValueError("target.code 为空：要先把股票解析成代码（ds.resolve_stock）")
    coverage_start, latest = ds.data_range(STOCK)
    start = max(spec.time_range.start, coverage_start)
    end = min(spec.time_range.end, latest)
    if start > end:
        raise MissingDataError(
            f"回看区间 {spec.time_range.start} ~ {spec.time_range.end} 不在本地数据（{coverage_start} ~ {latest}）里"
        )
    rows = ds.get_fields([code], coverage_start, latest, fields)
    universe = rows.filter(pl.col("date").is_between(start, end)).select("date", "code")
    if universe.is_empty():
        raise MissingDataError(f"本地没有 {code} 在 {start} ~ {end} 的行情")
    evaluation = evaluate(onset(parse(spec.event.expr)), "event", universe, start, end, ds)
    return coverage_start, latest, start, end, rows, evaluation


def _record(
    day: date, trades: dict[int, Trade], market: dict[tuple[date, int], tuple]
) -> TriggerRecord:
    entry = next(iter(trades.values()))  # 买入日和持有几天无关，各档都一样
    notes = dict.fromkeys(note for trade in trades.values() for note in trade.notes)
    return TriggerRecord(
        trigger_date=day,
        entry_date=entry.entry_date,
        entry_delay=entry.entry_delay,
        status={h: t.status for h, t in trades.items()},
        exit_date={h: t.exit_date for h, t in trades.items()},
        exit_delay={h: t.exit_delay for h, t in trades.items()},
        returns={h: t.ret for h, t in trades.items()},
        market_returns={h: market.get((day, h), (None, 0))[0] for h in trades},
        market_excluded={h: market.get((day, h), (None, 0))[1] for h in trades},
        notes=tuple(notes),
    )


def _market_returns(
    benchmark: str,
    trades: dict[date, dict[int, Trade]],
    coverage_start: date,
    latest: date,
    ds: DataService,
) -> dict[tuple[date, int], tuple[float | None, int]]:
    """每一笔计入平均的交易，同一段「买入日开盘 → 卖出日收盘」的对照涨跌与剔除只数。"""
    keys = [
        (day, h)
        for day, by_h in trades.items()
        for h, trade in by_h.items()
        if trade.status in COUNTED
    ]
    if not keys:
        return {}
    windows = pl.DataFrame(
        [
            (i, trades[day][h].entry_date, trades[day][h].exit_date)
            for i, (day, h) in enumerate(keys)
        ],
        schema={"window": pl.Int64, "buy_date": pl.Date, "sell_date": pl.Date},
        orient="row",
    )
    buy_days = sorted(set(windows.get_column("buy_date").to_list()))
    needed = sorted(set(buy_days) | set(windows.get_column("sell_date").to_list()))

    if benchmark.startswith("index:"):
        daily = ds.get_index_daily(benchmark.removeprefix("index:"), needed[0], needed[-1])
        result = (
            windows.join(
                daily.select(pl.col("date").alias("buy_date"), "open"), on="buy_date", how="left"
            )
            .join(
                daily.select(pl.col("date").alias("sell_date"), "close"), on="sell_date", how="left"
            )
            .select(
                "window",
                (pl.col("close") / pl.col("open") - 1).alias("market_return"),
                pl.lit(0).alias("excluded"),
            )
        )
    else:
        pool = ds.get_universe_mask(
            buy_days[0], buy_days[-1], exclude=list(BENCHMARK_EXCLUDE)
        ).filter(pl.col("date").is_in(buy_days))
        prices = ds.get_fields(None, needed[0], needed[-1], ["open", "close"]).filter(
            pl.col("date").is_in(needed)
        )
        result = pool_average(windows, pool, prices, _delisted(pool, latest, coverage_start, ds))

    values = {
        row["window"]: (row["market_return"], row["excluded"])
        for row in result.iter_rows(named=True)
    }
    return {key: values.get(i, (None, 0)) for i, key in enumerate(keys)}


def _delisted(
    pool: pl.DataFrame, latest: date, coverage_start: date, ds: DataService
) -> pl.DataFrame:
    """池子里退市股的最后一个交易日和收盘价。"""
    codes = pool.get_column("code").unique().to_list()
    gone = (
        ds.stock_info(codes, latest).filter(pl.col("delist_date").is_not_null()).get_column("code")
    )
    schema = {"code": pl.String, "last_date": pl.Date, "last_close": pl.Float64}
    if gone.is_empty():
        return pl.DataFrame(schema=schema)
    closes = ds.get_fields(gone.to_list(), coverage_start, latest, ["close"])
    return (
        closes.sort("code", "date")
        .group_by("code", maintain_order=True)
        .agg(pl.col("date").last().alias("last_date"), pl.col("close").last().alias("last_close"))
    )


def _summarize(
    horizon: int, records: tuple[TriggerRecord, ...], baseline: list[Trade], cost_bps: float
) -> HorizonSummary:
    counted = [r for r in records if r.status[horizon] in COUNTED]
    returns = [r.returns[horizon] for r in counted]
    markets = [r.market_returns[horizon] for r in counted]
    paired = [
        (ret, mkt)
        for ret, mkt in zip(returns, markets, strict=True)
        if ret is not None and mkt is not None
    ]
    mean_return = _mean(returns)
    return HorizonSummary(
        n=len(counted),
        mean_return=mean_return,
        mean_return_after_cost=None if mean_return is None else mean_return - cost_bps / 10_000,
        mean_market_return=_mean(markets),
        mean_baseline_return=_mean(t.ret for t in baseline if t.status in COUNTED),
        win_rate=sum(ret > mkt for ret, mkt in paired) / len(paired) if paired else None,
        unfilled=sum(r.status[horizon] == UNFILLED for r in records),
        pending=sum(r.status[horizon] == PENDING for r in records),
    )


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _notes(
    triggers: int, summary: dict[int, HorizonSummary], start: date, end: date, latest: date
) -> tuple[str, ...]:
    """固定模板加数字填空，不经过 LLM（§4.5）。"""
    notes: list[str] = []
    if triggers == 0:
        notes.append(f"{start} ~ {end} 里这个事件一次都没有触发")
    elif triggers < FEW_TRIGGERS:
        notes.append(f"仅触发 {triggers} 次，样本偏少，不能排除是运气")
    for horizon, item in summary.items():
        if item.unfilled:
            notes.append(f"持有 {horizon} 天：{item.unfilled} 次因长期停牌无法成交，未计入平均")
        if item.pending:
            notes.append(
                f"持有 {horizon} 天：{item.pending} 次还没走完（卖出日在 {latest} 之后），未计入平均"
            )
    return tuple(notes)
