"""算子清单：名字、参数、结果类型、窗口下限、预热条数、中文说明。

校验器、预热推导、给大模型的算子清单都从这里取，保证「清单里写的」和「校验器认的」是同一份（ARCHITECTURE §3.3）。
具体怎么算在 timeseries.py / cross_section.py / logic.py。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

#: 值的类型
NUM, BOOL = "num", "bool"

#: 单个窗口 n 的上限（约 4 年）
MAX_WINDOW = 1000

#: 整个表达式往前要读的行情条数（预热）上限。更长的表达式最坏要读近十年全市场数据，
#: 实测单次 4 秒多、2~3 GB 内存；上限 1000 时事件库和常见条件都在里面（§3.5）
MAX_LOOKBACK = 1000

#: EMA 按 8n 条预热。2026-09-14 实测 13 个日期、2867 次 MACD 金叉：4n 有 1 次判断和从上市算起不同，8n 一次不差
EMA_WARMUP = 8

TIMESERIES, CROSS_SECTION, ELEMENTWISE = "timeseries", "cross_section", "elementwise"

#: 参数类型：num 数值、bool 条件、any 都行、window 窗口天数（正整数字面量）、date 日期（YYYYMMDD 整数字面量）
WINDOW, ANY, DATE = "window", "any", "date"


@dataclass(frozen=True)
class Operator:
    name: str
    kind: str
    args: tuple[str, ...]
    returns: str  # num / bool / first（同第一个参数）/ branch（同 If 的分支）
    signature: str
    label: str
    min_window: int = 1
    #: 窗口天数 n → 这个算子自己要往前多读几条；没有窗口参数的为 None
    warmup: Callable[[int], int] | None = None

    @property
    def has_window(self) -> bool:
        return WINDOW in self.args


def _window_minus_one(n: int) -> int:  # 含当天的 n 天窗口，往前 n-1 条
    return n - 1


def _window(n: int) -> int:  # n 天前的值，往前 n 条
    return n


def _ema(n: int) -> int:
    return EMA_WARMUP * n


def _ts(name, signature, label, warmup, *, args=(NUM, WINDOW), returns=NUM, min_window=1):
    return Operator(name, TIMESERIES, args, returns, signature, label, min_window, warmup)


_OPERATORS: tuple[Operator, ...] = (
    # ── 时序算子：沿时间轴、逐标的，窗口数的是该标的自己有行情的交易日 ──
    _ts("Mean", "Mean(x, n)", "过去 n 个交易日的均值（含当天）", _window_minus_one),
    _ts("EMA", "EMA(x, n)", "指数移动平均，平滑系数 2/(n+1)", _ema),
    _ts(
        "Std",
        "Std(x, n)",
        "过去 n 个交易日的样本标准差（含当天），n 至少为 2",
        _window_minus_one,
        min_window=2,
    ),
    _ts("Sum", "Sum(x, n)", "过去 n 个交易日求和（含当天）", _window_minus_one),
    _ts("Max", "Max(x, n)", "过去 n 个交易日的最大值（含当天）", _window_minus_one),
    _ts("Min", "Min(x, n)", "过去 n 个交易日的最小值（含当天）", _window_minus_one),
    _ts(
        "Ref",
        "Ref(x, n)",
        "n 个交易日前的值，n 必须大于 0",
        _window,
        args=(ANY, WINDOW),
        returns="first",
    ),
    _ts("Delta", "Delta(x, n)", "x - Ref(x, n)", _window),
    _ts(
        "Pct",
        "Pct(x, n)",
        "x / Ref(x, n) - 1，返回小数（0.05 表示 5%）；要百分数直接用 $pct_chg",
        _window,
    ),
    # 2026-09-15 加：「今年以来涨幅」用 Pct($close, 170) 数条数，停过牌的股票会数到去年更早（有研硅、江丰电子实测算偏）
    Operator(
        "PctSince",
        TIMESERIES,
        (NUM, DATE),
        NUM,
        "PctSince(x, 20251231)",
        "x 较某一天的涨跌幅：和 YYYYMMDD 那天的值比（那天停牌就用它之前最后一个交易日），返回小数，那天及以前为空。"
        "「今年以来」「从 3 月 1 日起」用它，日期写起始日的前一天",
    ),
    _ts(
        "TsRank",
        "TsRank(x, n)",
        "当天的值在过去 n 个交易日里的分位：名次 ÷ n，并列取平均，最高为 1；n 至少为 2",
        _window_minus_one,
        min_window=2,
    ),
    _ts(
        "Count",
        "Count(cond, n)",
        "过去 n 个交易日里条件成立的天数（含当天）",
        _window_minus_one,
        args=(BOOL, WINDOW),
    ),
    Operator(
        "Cross",
        TIMESERIES,
        (NUM, NUM),
        BOOL,
        "Cross(x, y)",
        "上穿：前一个交易日 x <= y，当天 x > y",
    ),
    # ── 横截面算子 ──
    Operator(
        "Rank",
        CROSS_SECTION,
        (NUM,),
        NUM,
        "Rank(x)",
        "当天在股票池内的分位：名次 ÷ 个数，并列取平均，最高为 1；空值不参与。板块表是在当天全部板块里排",
    ),
    # ── 逐行计算 ──
    Operator(
        "If", ELEMENTWISE, (BOOL, ANY, ANY), "branch", "If(cond, a, b)", "条件成立取 a，否则取 b"
    ),
    Operator("Abs", ELEMENTWISE, (NUM,), NUM, "Abs(x)", "绝对值"),
    Operator("Log", ELEMENTWISE, (NUM,), NUM, "Log(x)", "自然对数；x <= 0 时为空值"),
    Operator("Sign", ELEMENTWISE, (NUM,), NUM, "Sign(x)", "正数为 1、负数为 -1、零为 0"),
)

OPERATORS: dict[str, Operator] = {op.name: op for op in _OPERATORS}
