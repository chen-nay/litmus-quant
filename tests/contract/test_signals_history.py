"""事件库在本地真实数据上的验收：15 个事件用默认参数各跑一次完整的个股回看（第 4 步验收标准）。

控制数据量：只用鼎信通讯（波动大，大多数事件都会触发）一年的区间，对照用沪深300，不读全市场。
没有本地数据就整个跳过。
"""

from __future__ import annotations

from datetime import date

import pytest

from litmus.data import DataService, MissingDataError
from litmus.research import HistoryResult, run
from litmus.signals import load_events, render_event
from litmus.spec import parse_spec

START, END = date(2025, 9, 1), date(2026, 9, 11)
STATUSES = {"完成", "退市", "无法成交", "观察中"}

_ds = DataService.from_env()
try:
    _first, _last = _ds.data_range()
except MissingDataError:
    pytest.skip("本地没有同步过的股票日频数据", allow_module_level=True)
if _last < END:
    pytest.skip(f"用例要到 {END} 的数据，本地只到 {_last}", allow_module_level=True)


@pytest.mark.parametrize("preset_id", [event.id for event in load_events().events])
def test_每个事件用默认参数都能跑出个股回看(preset_id):
    rendered = render_event(preset_id)
    spec = parse_spec(
        {
            "subject": {
                "kind": "codes",
                "codes": ["603421.SH"],
                "mentions": [{"mention": "鼎信通讯"}],
            },
            "when": {"range": {"from": START.isoformat(), "to": END.isoformat()}},
            "output": {
                "kind": "event_study",
                "event": {
                    "preset_id": rendered.preset_id,
                    "params": rendered.params,
                    "expr": rendered.expr,
                    "label": rendered.label,
                    "library_version": rendered.library_version,
                },
                "horizons": [5, 20],
                "benchmark": "index:000300.SH",
            },
            "defaults_used": [f"output.event.params.{name}" for name in rendered.defaults_used],
        }
    )
    result = run(spec, _ds)

    assert isinstance(result, HistoryResult)
    assert result.event_label == rendered.label
    assert result.range == (START, END)  # 预热在区间之前补足，统计从区间第一天开始
    assert set(result.summary) == {5, 20}
    assert all(item.status[h] in STATUSES for item in result.triggers for h in (5, 20))
