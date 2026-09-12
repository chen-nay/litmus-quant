"""Tushare 传输层的离线测试：用假 transport 造出分页和各类错误，不联网。"""

from __future__ import annotations

import httpx
import pytest

from litmus.data.loaders.tushare import (
    RateLimiter,
    TushareAuthError,
    TushareClient,
    TushareConfig,
    TushareError,
    TushareRateLimitError,
)

CONFIG = TushareConfig(token="t", base_url="https://example.invalid/")


def ok(fields: list[str], items: list[list]) -> dict:
    return {"code": 0, "msg": None, "data": {"fields": fields, "items": items}}


def err(msg: str, code: int = -1) -> dict:
    return {"code": code, "msg": msg, "data": None}


class FakeTransport:
    """按剧本依次返回响应；异常实例会被抛出。"""

    def __init__(self, *responses: object):
        self.responses = list(responses)
        self.payloads: list[dict] = []

    def __call__(self, url: str, payload: dict, timeout: float) -> dict:
        self.payloads.append(payload)
        if not self.responses:
            raise AssertionError("假 transport 的剧本用完了，说明多调了一次")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


def make_client(transport: FakeTransport, **kwargs) -> TushareClient:
    sleeps: list[float] = []
    client = TushareClient(
        CONFIG, transport=transport, limiter=RateLimiter(limits={}), sleep=sleeps.append, **kwargs
    )
    client.test_sleeps = sleeps  # type: ignore[attr-defined]
    return client


def test_单页取完就不再翻页():
    transport = FakeTransport(ok(["a", "b"], [[1, 2], [3, 4]]))
    rows = make_client(transport).call("daily", {"trade_date": "20260911"}, page_size=10)

    assert rows == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    assert len(transport.payloads) == 1
    assert transport.payloads[0]["params"]["limit"] == 10
    assert transport.payloads[0]["params"]["offset"] == 0


def test_取满一页就继续翻页_offset依次递增():
    transport = FakeTransport(
        ok(["a"], [[1], [2]]),
        ok(["a"], [[3], [4]]),
        ok(["a"], [[5]]),
    )
    rows = make_client(transport).call("daily", page_size=2)

    assert [r["a"] for r in rows] == [1, 2, 3, 4, 5]
    assert [p["params"]["offset"] for p in transport.payloads] == [0, 2, 4]


def test_接口不支持offset分页时停下并告警():
    page = ok(["a"], [[1], [2]])
    transport = FakeTransport(page, dict(page))
    rows = make_client(transport).call("daily", page_size=2)

    assert [r["a"] for r in rows] == [1, 2]
    assert len(transport.payloads) == 2


def test_权限不足抛AuthError且不重试():
    transport = FakeTransport(err("抱歉，您没有接口访问权限，需要2000积分"))
    client = make_client(transport)

    with pytest.raises(TushareAuthError):
        client.call("tdx_index", page_size=10)
    assert len(transport.payloads) == 1


def test_频率限制会退避后重试():
    transport = FakeTransport(
        err("抱歉，您每分钟最多访问该接口500次"),
        ok(["a"], [[1]]),
    )
    client = make_client(transport)
    rows = client.call("daily", page_size=10)

    assert rows == [{"a": 1}]
    assert len(transport.payloads) == 2
    assert client.test_sleeps  # 确实等待过


def test_网络错误重试耗尽后抛错():
    transport = FakeTransport(*[httpx.ConnectError("boom")] * 4)
    client = make_client(transport)

    with pytest.raises(TushareError, match="重试"):
        client.call("daily", page_size=10)
    assert len(transport.payloads) == 4  # 1 次 + 3 次重试


def test_返回结构异常直接报错():
    transport = FakeTransport({"code": 0, "msg": None, "data": {}})
    with pytest.raises(TushareError, match="fields"):
        make_client(transport).call("daily", page_size=10)


def test_行宽与字段数不一致直接报错():
    transport = FakeTransport(ok(["a", "b"], [[1]]))
    with pytest.raises(TushareError, match="行宽"):
        make_client(transport).call("daily", page_size=10)


def test_未分类的错误码原样抛出():
    transport = FakeTransport(err("系统内部错误", code=500))
    with pytest.raises(TushareError) as exc_info:
        make_client(transport).call("daily", page_size=10)
    assert not isinstance(exc_info.value, TushareAuthError | TushareRateLimitError)


def test_probe_权限不足返回不可用():
    transport = FakeTransport(err("抱歉，您没有接口访问权限，需要6000积分"))
    available, reason = make_client(transport).probe("tdx_index")

    assert available is False
    assert "6000积分" in reason


def test_probe_可用时返回True():
    transport = FakeTransport(ok(["a"], [[1]]))
    assert make_client(transport).probe("tdx_index") == (True, "")


def test_probe_遇到非权限错误不吞掉():
    transport = FakeTransport(err("系统内部错误", code=500))
    with pytest.raises(TushareError):
        make_client(transport).probe("tdx_index")


def test_限速器到达上限会等待():
    now = [0.0]
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(limits={"daily": 2}, clock=lambda: now[0], sleep=sleep)
    limiter.acquire("daily")
    limiter.acquire("daily")
    limiter.acquire("daily")  # 第 3 次必须等到窗口滑出

    assert len(waits) == 1
    assert waits[0] == pytest.approx(60.0, abs=0.1)


def test_from_env_缺token直接报错():
    with pytest.raises(TushareError, match="TUSHARE_TOKEN"):
        TushareConfig.from_env({})


def test_from_env_未配地址时用官方地址():
    config = TushareConfig.from_env({"TUSHARE_TOKEN": "x", "TUSHARE_HTTP_URL": ""})
    assert config.base_url == "http://api.tushare.pro"
