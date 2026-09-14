"""DataService 不需要行情数据就能测的部分：参数校验、能力开关、没同步过。

读真实数据的契约在 tests/contract/test_dataservice.py。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from litmus.data.fields import CONCEPT_CAPABILITY, UnknownFieldError
from litmus.data.manifest import Manifest
from litmus.data.service import DataService
from litmus.data.storage import MarketStore, MissingDataError

DAY = date(2026, 9, 11)


@pytest.fixture
def ds(tmp_path: Path) -> DataService:
    return DataService(MarketStore(tmp_path))


def test_没同步过时最新交易日报错(ds):
    with pytest.raises(MissingDataError, match="还没有股票日频"):
        ds.latest_trading_day()


def test_不认识的字段直接报错(ds):
    with pytest.raises(UnknownFieldError, match="foo"):
        ds.get_fields(None, DAY, DAY, ["foo"])


def test_股票专有字段不能用在板块上(ds):
    with pytest.raises(UnknownFieldError, match="pe_ttm"):
        ds.get_fields(None, DAY, DAY, ["pe_ttm"], target="sw_industry")


def test_内部列只给股票用(ds):
    with pytest.raises(UnknownFieldError, match="up_limit"):
        ds.get_fields(None, DAY, DAY, ["up_limit"], target="sw_industry")


def test_不认识的标的类型直接报错(ds):
    with pytest.raises(ValueError, match="标的类型"):
        ds.get_fields(None, DAY, DAY, ["close"], target="fund")


def test_字段不能为空(ds):
    with pytest.raises(ValueError, match="fields"):
        ds.get_fields(None, DAY, DAY, [])


def test_起始日晚于结束日直接报错(ds):
    with pytest.raises(ValueError, match="晚于"):
        ds.get_fields(None, DAY, date(2026, 9, 1), ["close"])


def test_不认识的股票池直接报错(ds):
    with pytest.raises(ValueError, match="sz50"):
        ds.get_universe_mask(DAY, DAY, base="sz50")


def test_概念板块不可用时报错并说明原因(tmp_path):
    store = MarketStore(tmp_path)
    manifest = Manifest()
    manifest.record_capability(CONCEPT_CAPABILITY, False, "需要 6000 积分")
    manifest.save(store)
    ds = DataService(store)

    with pytest.raises(MissingDataError, match="6000 积分"):
        ds.get_fields(None, DAY, DAY, ["close"], target="concept")
    with pytest.raises(MissingDataError, match="6000 积分"):
        ds.list_boards("concept")
