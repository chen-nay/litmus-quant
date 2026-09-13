"""字段目录：所有字段的唯一来源。

表达式的字段白名单、给 LLM 的字段清单，都从这里出，保证"LLM 以为有的"和"校验器认的"
永远是同一份。字段含义、单位、来源与换算方式见 ARCHITECTURE.md §2.2。

单位约定：金额一律为元，股数一律为股，比率保持百分数（换手率 5 表示 5%），估值为倍数。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: 标的类型
STOCK = "stock"
SW_INDUSTRY = "sw_industry"
CONCEPT = "concept"
BOARD_TARGETS = (SW_INDUSTRY, CONCEPT)


@dataclass(frozen=True)
class Field:
    """一个可以出现在表达式里的字段。"""

    name: str  # 表达式里写作 $name
    label: str  # 中文名
    unit: str  # 元 / 股 / % / 倍 / 点 / 个 / 布尔
    dtype: str  # float / bool
    targets: frozenset[str]  # 哪些标的类型有这个字段
    note: str = ""
    time_series_ok: bool = True  # 能否进入时序算子（Mean / Ref / Cross 等）

    def available_for(self, target: str) -> bool:
        return target in self.targets


def _f(name, label, unit, dtype, targets, note="", time_series_ok=True) -> Field:
    return Field(name, label, unit, dtype, frozenset(targets), note, time_series_ok)


_STOCK_ONLY = (STOCK,)
_ALL_BOARDS = BOARD_TARGETS

_FIELD_LIST: tuple[Field, ...] = (
    # ── 行情 ──────────────────────────────────────────────────
    _f("open", "开盘价", "元", "float", (STOCK, *_ALL_BOARDS), "股票为后复权；板块为点位"),
    _f("high", "最高价", "元", "float", (STOCK, *_ALL_BOARDS), "股票为后复权；板块为点位"),
    _f("low", "最低价", "元", "float", (STOCK, *_ALL_BOARDS), "股票为后复权；板块为点位"),
    _f("close", "收盘价", "元", "float", (STOCK, *_ALL_BOARDS), "股票为后复权；板块为点位"),
    _f(
        "close_raw",
        "收盘价（不复权）",
        "元",
        "float",
        _STOCK_ONLY,
        "真实股价，用于「股价低于 10 元」这类绝对价格条件；除权日会断崖下跌，禁止进时序算子",
        time_series_ok=False,
    ),
    _f("volume", "成交量", "股", "float", _STOCK_ONLY, "随复权调整：原始股数 ÷ 复权因子"),
    _f("amount", "成交额", "元", "float", (STOCK, *_ALL_BOARDS)),
    _f(
        "vwap",
        "成交均价",
        "元",
        "float",
        _STOCK_ONLY,
        "成交额 ÷ 成交股数 × 复权因子，与 close 同口径",
    ),
    _f("pct_chg", "当日涨跌幅", "%", "float", (STOCK, *_ALL_BOARDS), "百分数：5 表示 5%"),
    _f("turnover", "换手率", "%", "float", (STOCK, CONCEPT)),
    # ── 估值与规模 ────────────────────────────────────────────
    _f("pe_ttm", "市盈率TTM", "倍", "float", _STOCK_ONLY, "亏损股为空值"),
    _f("pb", "市净率", "倍", "float", (STOCK, *_ALL_BOARDS)),
    _f("ps_ttm", "市销率TTM", "倍", "float", _STOCK_ONLY),
    _f("dv_ttm", "股息率TTM", "%", "float", _STOCK_ONLY),
    _f("market_cap", "总市值", "元", "float", (STOCK, SW_INDUSTRY), "概念板块口径不同，不提供"),
    _f("circ_mv", "流通市值", "元", "float", (STOCK, *_ALL_BOARDS)),
    # ── 财务（按披露日对齐）────────────────────────────────────
    _f("roe", "净资产收益率（年化）", "%", "float", _STOCK_ONLY),
    _f("revenue_yoy", "营业收入同比", "%", "float", _STOCK_ONLY, "最新一期累计同比，披露日会跳变"),
    _f("profit_yoy", "归母净利润同比", "%", "float", _STOCK_ONLY, "最新一期累计同比，披露日会跳变"),
    # ── 事件 ──────────────────────────────────────────────────
    _f("is_report_date", "财报实际披露日", "布尔", "bool", _STOCK_ONLY),
    _f("is_forecast_date", "业绩预告公告日", "布尔", "bool", _STOCK_ONLY),
    _f("is_unlock_date", "限售解禁日", "布尔", "bool", _STOCK_ONLY),
    _f("is_ex_div", "除权除息日", "布尔", "bool", _STOCK_ONLY, "复权因子较前一交易日发生变化"),
    # ── 状态 ──────────────────────────────────────────────────
    _f("is_st", "ST / *ST", "布尔", "bool", _STOCK_ONLY),
    _f("is_limit_up", "收盘涨停", "布尔", "bool", _STOCK_ONLY),
    _f("is_limit_down", "收盘跌停", "布尔", "bool", _STOCK_ONLY),
    _f("is_new", "次新股", "布尔", "bool", _STOCK_ONLY),
    # ── 板块特有 ──────────────────────────────────────────────
    _f("up_num", "上涨家数", "个", "float", (CONCEPT,)),
    _f("limit_up_num", "涨停家数", "个", "float", (CONCEPT,)),
)

FIELDS: dict[str, Field] = {f.name: f for f in _FIELD_LIST}

#: 不对表达式开放、只给 research 用的内部列
INTERNAL_COLUMNS: frozenset[str] = frozenset(
    {"adj_factor", "up_limit", "down_limit", "open_limit_up"}
)


class UnknownFieldError(KeyError):
    """请求了 FIELDS 之外的字段。"""


def get(name: str) -> Field:
    """按字段名取定义。表达式里的 `$close` 在这里是 "close"。"""
    try:
        return FIELDS[name]
    except KeyError as exc:
        raise UnknownFieldError(f"没有字段 ${name}") from exc


def names_for(target: str = STOCK) -> tuple[str, ...]:
    """某类标的可用的字段名，按定义顺序。"""
    return tuple(f.name for f in _FIELD_LIST if f.available_for(target))


def check_available(names: Iterable[str], target: str = STOCK) -> None:
    """校验一批字段名对该标的是否可用，不可用就报错——绝不返回空列糊弄过去。"""
    for name in names:
        f = get(name)
        if not f.available_for(target):
            raise UnknownFieldError(
                f"字段 ${name} 对 {target} 不可用（只用于 {sorted(f.targets)}）"
            )
