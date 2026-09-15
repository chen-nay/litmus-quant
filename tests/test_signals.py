"""事件库的测试：15 个事件都能生成、参数怎么核对、事件库文件写错时启动就报错。不读行情数据。"""

from __future__ import annotations

import copy
import tomllib
from importlib import resources

import pytest

from litmus.expr import collect_lookback, onset, parse
from litmus.signals import (
    EventDefinitionError,
    EventParamError,
    event_catalog,
    load_events,
    parse_library,
    render_event,
)
from litmus.spec import DEFAULTS

IDS = [
    "breakout_ma",
    "breakdown_ma",
    "ma_golden_cross",
    "macd_golden_cross",
    "volume_surge",
    "breakout_ma_volume",
    "shrink_pullback",
    "new_high",
    "new_low",
    "limit_up",
    "consecutive_limit_up",
    "limit_down",
    "report_date",
    "forecast_date",
    "ex_dividend",
]


def raw_library() -> dict:
    text = resources.files("litmus.signals").joinpath("builtin.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


def raw_event(raw: dict, preset_id: str) -> dict:
    return next(event for event in raw["events"] if event["id"] == preset_id)


# ── 事件库本身 ──────────────────────────────────────────────────


def test_十五个事件_顺序和文档一致_带版本号():
    library = load_events()
    assert [event.id for event in library.events] == IDS
    assert library.version >= 1


@pytest.mark.parametrize("preset_id", IDS)
def test_每个事件用默认参数都能生成(preset_id):
    rendered = render_event(preset_id)
    assert rendered.library_version == load_events().version
    assert rendered.defaults_used == tuple(rendered.params)
    assert (
        collect_lookback(onset(parse(rendered.expr))) <= 282
    )  # 最多的是 MACD 金叉：281 + 事件包装 1 条


def test_放量突破均线就是突破均线和单日放量同时成立():
    both = render_event("breakout_ma_volume", {"ma": 60, "volume_ratio": 3})
    breakout = render_event("breakout_ma", {"ma": 60})
    surge = render_event("volume_surge", {"volume_ratio": 3})
    assert both.expr == f"({breakout.expr}) & ({surge.expr})"


def test_默认值和默认值表一致():
    def default(preset_id: str, name: str):
        return render_event(preset_id).params[name]

    assert default("volume_surge", "volume_ratio") == DEFAULTS["volume_surge_ratio"]
    assert default("breakout_ma_volume", "volume_ratio") == DEFAULTS["volume_surge_ratio"]
    assert default("shrink_pullback", "shrink_ratio") == DEFAULTS["volume_shrink_ratio"]
    assert default("breakout_ma", "ma") == DEFAULTS["year_days"]
    assert default("new_high", "days") == DEFAULTS["year_days"]
    assert default("ma_golden_cross", "fast") == DEFAULTS["week_days"]
    assert default("ma_golden_cross", "slow") == DEFAULTS["month_days"]
    assert f"Ref($amount, 1), {DEFAULTS['month_days']})" in render_event("volume_surge").expr


def test_示例参数都合法_问句不为空():
    for event in load_events().events:
        render_event(event.id, event.example.params)
        assert event.example.question


# ── 生成 ────────────────────────────────────────────────────────


def test_代入参数_没填的用默认值并记下来():
    rendered = render_event("breakout_ma_volume", {"volume_ratio": 3})
    assert rendered.params == {"ma": 250, "volume_ratio": 3.0}
    assert rendered.defaults_used == ("ma",)
    assert rendered.expr == (
        "(Cross($close, Mean($close, 250))) & ($amount > Mean(Ref($amount, 1), 20) * 3.0)"
    )
    assert rendered.label == "放量突破 250 日均线（成交额超过前 20 日均额的 3 倍）"


def test_标签里的小数():
    assert render_event("volume_surge", {"volume_ratio": 2.5}).label.endswith("2.5 倍")
    assert render_event("shrink_pullback").label.endswith("0.5 倍")


def test_整数参数写成60点0也行():
    rendered = render_event("breakout_ma", {"ma": 60.0})
    assert rendered.params == {"ma": 60}
    assert "Mean($close, 60))" in rendered.expr


# ── 参数核对 ────────────────────────────────────────────────────


def param_error(preset_id: str, params: dict) -> EventParamError:
    with pytest.raises(EventParamError) as info:
        render_event(preset_id, params)
    return info.value


def test_不在可选值里_说明可选范围():
    error = param_error("breakout_ma", {"ma": 30})
    assert str(error) == "「突破均线」的均线天数（ma）可选 5/10/20/60/120/250，收到 30"
    assert (error.preset_id, error.param, error.value, error.allowed) == (
        "breakout_ma",
        "ma",
        30,
        "5/10/20/60/120/250",
    )


def test_小数参数的范围含两端():
    assert render_event("volume_surge", {"volume_ratio": 1.2}).params["volume_ratio"] == 1.2
    assert render_event("volume_surge", {"volume_ratio": 10}).params["volume_ratio"] == 10.0
    assert param_error("volume_surge", {"volume_ratio": 10.5}).allowed == "1.2~10"
    assert param_error("shrink_pullback", {"shrink_ratio": 1.0}).allowed == "0.1~0.9"


def test_整数参数的范围():
    assert (
        render_event("consecutive_limit_up", {"boards": 10}).expr == "Count($is_limit_up, 10) == 10"
    )
    assert param_error("consecutive_limit_up", {"boards": 11}).allowed == "2~10"
    assert "要是整数" in str(param_error("consecutive_limit_up", {"boards": 2.5}))


@pytest.mark.parametrize("value", [True, "60", None, float("nan")])
def test_不是数字的参数(value):
    assert "要是数字" in str(param_error("breakout_ma", {"ma": value}))


def test_不认识的参数():
    error = param_error("breakout_ma", {"days": 60})
    assert "没有参数 days" in str(error) and "ma（均线天数）" in str(error)
    assert "可调的参数：无" in str(param_error("limit_up", {"boards": 2}))


def test_不认识的事件():
    with pytest.raises(EventParamError, match="breakout_ma（突破均线）"):
        render_event("breakout_year_line")


def test_短均线要小于长均线():
    error = param_error("ma_golden_cross", {"fast": 20, "slow": 20})
    assert str(error) == "「均线金叉」的短均线天数（fast=20）要小于长均线天数（slow=20）"
    assert error.param == "fast"
    assert (
        render_event("ma_golden_cross", {"fast": 60, "slow": 250}).label
        == "60 日均线上穿 250 日均线"
    )


# ── 事件库文件写错时启动就报错 ──────────────────────────────────


def broken(change) -> dict:
    raw = copy.deepcopy(raw_library())
    change(raw)
    return raw


def test_模板占位符和参数对不上():
    def change(raw):
        raw_event(raw, "breakout_ma")["template"] = "Cross($close, Mean($close, {days}))"

    with pytest.raises(EventDefinitionError, match="占位符"):
        parse_library(broken(change))


def test_默认值不在范围里():
    def change(raw):
        raw_event(raw, "volume_surge")["params"]["volume_ratio"]["default"] = 20.0

    with pytest.raises(EventDefinitionError, match="默认值"):
        parse_library(broken(change))


def test_某个参数组合预热超过上限():
    """默认值合法，但可选值里有一个会让预热超过 1000 条，加载时就要拦下。"""

    def change(raw):
        new_high = raw_event(raw, "new_high")
        new_high["template"] = "$close >= EMA($close, {days})"
        new_high["params"]["days"]["choices"] = [20, 250]
        new_high["params"]["days"]["default"] = 20

    with pytest.raises(EventDefinitionError, match="days.*250.*往前读 2001 条"):
        parse_library(broken(change))


def test_模板里用了不存在的字段():
    def change(raw):
        raw_event(raw, "limit_up")["template"] = "$is_limit_upp"

    with pytest.raises(EventDefinitionError, match="没有字段"):
        parse_library(broken(change))


def test_文件里多写了不认识的字段():
    def change(raw):
        raw_event(raw, "limit_up")["description"] = "多写的"

    with pytest.raises(EventDefinitionError, match="description"):
        parse_library(broken(change))


def test_事件编号重复():
    def change(raw):
        raw_event(raw, "limit_down")["id"] = "limit_up"

    with pytest.raises(EventDefinitionError, match="重复"):
        parse_library(broken(change))


# ── 清单 ────────────────────────────────────────────────────────


def test_事件清单():
    catalog = event_catalog()
    assert [item["id"] for item in catalog] == IDS
    golden = next(item for item in catalog if item["id"] == "ma_golden_cross")
    assert golden["params"][0] == {
        "name": "fast",
        "label": "短均线天数",
        "unit": "天",
        "allowed": "5/10/20/60",
        "default": 5,
        "kind": "choice",
        "choices": [5, 10, 20, 60],
        "min": None,
        "max": None,
    }
    assert golden["constraints"] == ["短均线天数要小于长均线天数"]
    assert golden["example"]["params"] == {"fast": 5, "slow": 20}
