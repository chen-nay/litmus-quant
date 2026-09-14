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
