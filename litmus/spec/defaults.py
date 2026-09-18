"""默认值表与数值范围（ARCHITECTURE §5.3）。

默认值放在代码里而不是写死在提示词文字中：改一次到处生效、可以写测试、确认卡上能标出「这是默认值」。
生成提示词时把这张表渲染进去，LLM 看到的默认值和代码里的永远是同一份。
"""

from __future__ import annotations

DEFAULTS: dict[str, object] = {
    "volume_surge_ratio": 2.0,  # 「放量」= 前 20 日均额的几倍
    "volume_shrink_ratio": 0.5,  # 「缩量」
    "week_days": 5,  # 「1周」= 几个交易日
    "month_days": 20,
    "quarter_days": 60,
    "year_days": 250,
    "top_n": 50,  # 「前N名」没说 N 时取多少
    "volume_means": "amount",  # 「成交量」默认理解为成交额
    "cost_bps": 30,  # 交易成本，买卖双边合计
    "horizons": (5, 20, 60),  # 个股回看默认看之后几个交易日
    "exclude": ("ST", "suspended", "new_listing_60d"),  # 股票池默认剔除
    "benchmark": "universe_equal_weight",  # 个股回看默认对照：买入日全A等权
    "small_cap": 3_000_000_000,  # 「小市值」没给数字时：总市值低于多少元（30 亿，2026-09-15 定）
}

#: 股票表、板块表最多取前多少名
MAX_LIMIT = 500

#: 个股回看最多看之后多少个交易日（约一年）、最多几档
MAX_HORIZON = 250
MAX_HORIZONS = 10

#: 交易成本上限（基点）：5%，再高就不是正常的交易成本了
MAX_COST_BPS = 500

#: 个股回看可选的对照口径
BENCHMARKS: tuple[str, ...] = ("universe_equal_weight", "index:000300.SH", "index:000905.SH")

#: 卡上涨跌的同期对照：industry 是这只股票所属的申万一级行业指数（默认），或者宽基指数
CARD_BENCHMARKS: tuple[str, ...] = ("industry", "index:000300.SH", "index:000905.SH")

#: 卡的默认指标组：用户问开放问题（「最近走势如何」）、没点名看哪些数时，塞进提示词当建议，
#: 大模型可以直接用、可以删、可以加（DESIGN.md §1.5）。估值、涨跌、量能、财务各两个，
#: 估值的两年分位在卡上并进市盈率、市净率的解释行。{since_new_year} 由提示词填成去年最后一个交易日
DEFAULT_CARD_METRICS: tuple[tuple[str, str], ...] = (
    ("市盈率TTM", "$pe_ttm"),
    ("市盈率两年分位", "TsRank($pe_ttm, 500)"),
    ("市净率", "$pb"),
    ("市净率两年分位", "TsRank($pb, 500)"),
    ("近 20 日涨跌", "Pct($close, 20)"),
    ("今年以来涨跌", "PctSince($close, {since_new_year})"),
    ("换手率", "$turnover"),
    ("成交额放大倍数", "$amount / Mean(Ref($amount, 1), 20)"),
    ("营收同比", "$revenue_yoy"),
    ("净利同比", "$profit_yoy"),
)
