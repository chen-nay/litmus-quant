"""Tushare HTTP 接口的传输层。

只负责把数据原样取回来：调用、分页、限速、重试、错误分类、能力探测。
单位换算与字段归一是下一片的事，这里不碰。

接口定义以本地 `api_define/` 为准，写代码时只抄不猜。
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

OFFICIAL_BASE_URL = "http://api.tushare.pro"

#: 各接口单次返回的行数上限。请求的 limit 绝不能超过它，否则接口只返回自己的上限，
#: 而我们会把"少于 limit"误判成"取完了"，静默丢数据。
#:
#: 登记规则：只登记**证明安全**的值。api_define 里写明的直接抄；没写明的用实测——
#: 必须亲眼见过某一页正好返回这么多行（rows == limit），才能登记这个值。
#: 例：adj_factor 用 limit=6000 请求时返回 5562 行（当天全部），只能证明上限 ≥ 5562，
#: 所以登记 5000；登记 6000 的话，万一真实上限是 5800，超过 6000 行的日子就会被静默截断。
PAGE_SIZES: Mapping[str, int] = {
    # 来自 api_define/ 的明确说明
    "daily": 6000,
    "daily_basic": 6000,
    "stk_limit": 5800,
    "stock_basic": 6000,
    "stock_st": 1000,
    "sw_daily": 4000,
    "index_daily": 8000,
    "index_member_all": 2000,
    "disclosure_date": 3000,
    "share_float": 6000,
    "fina_indicator": 100,
    "tdx_index": 1000,
    "tdx_daily": 3000,
    "tdx_member": 3000,
    # 文档写"未明确限制"，2026-09-12 实测得出
    "adj_factor": 5000,  # 单日全市场 5562 行，上限 ≥ 5562
    "fina_indicator_vip": 6000,  # 单报告期请求 6000 正好返回 6000
    "forecast_vip": 4000,  # 单报告期请求 4000 正好返回 4000
    "namechange": 6000,  # 全量请求 6000 正好返回 6000
    "trade_cal": 4000,  # 2016 至今 4018 行一页返回
    "index_weight": 1000,  # 沪深300 月度 300 行、中证500 月度 500 行
    "index_classify": 1000,  # 申万一级共 31 行
}

#: 未登记上限的接口按这个保守值分页
DEFAULT_PAGE_SIZE = 1000

#: 各接口每分钟调用上限（来自 api_define/），未登记的用 DEFAULT_RATE_LIMIT
RATE_LIMITS: Mapping[str, int] = {
    "stock_basic": 50,
    "daily": 500,
}
DEFAULT_RATE_LIMIT = 400

#: 退避节奏，两种错误分开对待。
#: 网络抖动很快就恢复，几秒钟重试就够。
TRANSPORT_BACKOFF = (1.0, 2.0, 4.0)
#: 限流是**按分钟计的配额**，等几秒毫无意义——必须等到分钟窗口滚过去。
#: 2026-09-13 实测：disclosure_date 与 share_float 在 1/2/4 秒的退避下重试四次全部失败，
#: 整次同步被拖垮；改成按分钟等待才能跨过配额窗口。
RATE_LIMIT_BACKOFF = (15.0, 30.0, 60.0, 60.0)

#: 一次 call() 最多翻多少页。
#: 2026-09-13 实测代理的 offset 上限在 10 万上下：offset=60000 正常，offset=102000 返回
#: 「参数校验失败, offset」，也就是**第 18 页**就会被拒。阈值必须落在它前面，否则我们这条
#: 报错永远轮不到触发，用户看到的只有代理那句看不懂的参数校验失败。
#: 正常请求都在几页以内（最大的 namechange 全量也才 4 页），翻过 16 页就说明区间切得太粗。
MAX_PAGES = 16

#: 流控提示词。除了按分钟计的频率限制，代理还会限制同时在飞的连接数：
#: 2026-09-13 实测 12 路并发时返回 code=429 msg=请勿使用过多线程，连接超限
_RATE_LIMIT_HINTS = ("每分钟", "频率", "频次", "太频繁", "超限", "线程", "连接数")
#: 流控错误码。按字符串比对，代理返回的可能是数字也可能是字符串
_RATE_LIMIT_CODES = ("429",)
_PERMISSION_HINTS = ("积分", "权限", "没有访问", "token")


class TushareError(RuntimeError):
    """Tushare 调用失败。"""

    def __init__(self, message: str, *, api_name: str | None = None, code: object = None):
        super().__init__(message)
        self.api_name = api_name
        self.code = code


class TushareAuthError(TushareError):
    """积分或权限不足。不重试——能力探测据此判定某项数据不可用。"""


class TushareRateLimitError(TushareError):
    """触发频率限制。可以退避后重试。"""


#: (url, payload, timeout) -> 解析后的 JSON。抽出来是为了测试时注入假实现
Transport = Callable[[str, dict, float], dict]


@dataclass(frozen=True)
class TushareConfig:
    token: str
    base_url: str = OFFICIAL_BASE_URL
    timeout: float = 30.0
    max_retries: int = 3

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TushareConfig:
        """从环境变量读配置。TUSHARE_HTTP_URL 为空时用官方地址。"""
        env = os.environ if env is None else env
        token = (env.get("TUSHARE_TOKEN") or "").strip()
        if not token:
            raise TushareError("缺少 TUSHARE_TOKEN，请在 .env 中配置")
        base_url = (env.get("TUSHARE_HTTP_URL") or "").strip() or OFFICIAL_BASE_URL
        return cls(token=token, base_url=base_url)


class RateLimiter:
    """按接口分别限速的滑动窗口。clock / sleep 可注入，便于测试。"""

    def __init__(
        self,
        limits: Mapping[str, int] | None = None,
        default: int = DEFAULT_RATE_LIMIT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._limits = dict(limits if limits is not None else RATE_LIMITS)
        self._default = default
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._hits: MutableMapping[str, list[float]] = {}

    def acquire(self, api_name: str) -> None:
        limit = self._limits.get(api_name, self._default)
        while True:
            with self._lock:
                now = self._clock()
                hits = [t for t in self._hits.get(api_name, []) if now - t < 60.0]
                self._hits[api_name] = hits
                if len(hits) < limit:
                    hits.append(now)
                    return
                wait = 60.0 - (now - hits[0]) + 0.01
            logger.debug("接口 %s 触发本地限速，等待 %.2fs", api_name, wait)
            self._sleep(wait)


def _httpx_transport(client: httpx.Client) -> Transport:
    def send(url: str, payload: dict, timeout: float) -> dict:
        response = client.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()

    return send


class TushareClient:
    """一个 Tushare 接入点。可以多线程共用：httpx.Client 线程安全，限速器带锁。

    并发调度归 DataSync 管，这里不自己开线程。实测代理最多 8 路，再多会返回 429 连接超限。
    """

    def __init__(
        self,
        config: TushareConfig,
        transport: Transport | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._config = config
        self._own_client: httpx.Client | None = None
        if transport is None:
            self._own_client = httpx.Client(timeout=config.timeout)
            transport = _httpx_transport(self._own_client)
        self._transport = transport
        self._limiter = limiter if limiter is not None else RateLimiter()
        self._sleep = sleep

    # ── 生命周期 ────────────────────────────────────────────────
    def close(self) -> None:
        if self._own_client is not None:
            self._own_client.close()
            self._own_client = None

    def __enter__(self) -> TushareClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── 单页调用 ────────────────────────────────────────────────
    def call_page(
        self, api_name: str, params: dict | None = None, fields: str | None = None
    ) -> tuple[list[str], list[list]]:
        """调一次接口，返回 (字段名, 行)。失败按类型抛出，不静默降级。"""
        payload = {
            "api_name": api_name,
            "token": self._config.token,
            "params": dict(params or {}),
            "fields": fields or "",
        }
        # 两种错误各记各的次数：网络抖动和配额用尽是两回事，混在一起数会让
        # 一次网络抖动吃掉限流的重试预算
        transport_left = self._config.max_retries
        rate_limit_left = len(RATE_LIMIT_BACKOFF)
        while True:
            self._limiter.acquire(api_name)
            try:
                body = self._transport(self._config.base_url, payload, self._config.timeout)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                transport_left = self._wait_or_raise(
                    api_name, exc, transport_left, TRANSPORT_BACKOFF
                )
                continue
            try:
                return self._parse(api_name, body)
            except TushareRateLimitError as exc:
                rate_limit_left = self._wait_or_raise(
                    api_name, exc, rate_limit_left, RATE_LIMIT_BACKOFF
                )

    def _wait_or_raise(
        self, api_name: str, exc: Exception, left: int, schedule: tuple[float, ...]
    ) -> int:
        """按 schedule 退避一次，返回剩余次数；用完了就抛错。"""
        if left <= 0:
            raise TushareError(
                f"{api_name}: 重试 {len(schedule) + 1} 次仍失败：{exc}", api_name=api_name
            )
        wait = schedule[len(schedule) - left] + random.uniform(0, 0.5)
        logger.warning(
            "接口 %s 失败（%s），%.1fs 后重试（还剩 %d 次）", api_name, exc, wait, left - 1
        )
        self._sleep(wait)
        return left - 1

    def _parse(self, api_name: str, body: object) -> tuple[list[str], list[list]]:
        if not isinstance(body, dict) or "code" not in body:
            raise TushareError(
                f"{api_name}: 返回不是预期的 JSON 结构：{body!r:.200}", api_name=api_name
            )
        code = body.get("code")
        msg = str(body.get("msg") or "")
        if code != 0:
            raise self._classify(api_name, code, msg)
        data = body.get("data")
        if not isinstance(data, dict):
            raise TushareError(f"{api_name}: 返回缺少 data", api_name=api_name, code=code)
        fields, items = data.get("fields"), data.get("items")
        if not isinstance(fields, list) or not isinstance(items, list):
            raise TushareError(f"{api_name}: data 里缺少 fields 或 items", api_name=api_name)
        return fields, items

    @staticmethod
    def _classify(api_name: str, code: object, msg: str) -> TushareError:
        # 先判频率再判权限：频率提示里也可能出现"访问"字样
        if str(code) in _RATE_LIMIT_CODES or any(hint in msg for hint in _RATE_LIMIT_HINTS):
            return TushareRateLimitError(f"{api_name}: {msg}", api_name=api_name, code=code)
        if any(hint in msg for hint in _PERMISSION_HINTS):
            return TushareAuthError(f"{api_name}: {msg}", api_name=api_name, code=code)
        return TushareError(f"{api_name}: code={code} msg={msg}", api_name=api_name, code=code)

    # ── 自动分页 ────────────────────────────────────────────────
    def call(
        self,
        api_name: str,
        params: dict | None = None,
        fields: str | None = None,
        page_size: int | None = None,
    ) -> list[dict]:
        """调接口并自动翻页，返回行的列表。

        规则：返回行数等于请求的 limit 就继续翻页，绝不把"正好取满"当成"取完了"。
        """
        size = page_size or PAGE_SIZES.get(api_name)
        if size is None:
            size = DEFAULT_PAGE_SIZE
            logger.warning(
                "接口 %s 未登记单次上限，按保守值 %d 分页；请在 PAGE_SIZES 中补上", api_name, size
            )

        rows: list[dict] = []
        offset = 0
        previous_first: list | None = None
        for _ in range(MAX_PAGES):
            # offset=0 就是默认值，不发白不发；深翻页才带上它。
            # 代理对 offset 有上限：2026-09-13 实测 share_float 的 offset=60000 正常、
            # offset=300000 返回「参数校验失败, offset」——翻得太深会被拒。
            page_params = {**(params or {}), "limit": size}
            if offset:
                page_params["offset"] = offset
            field_names, items = self.call_page(api_name, page_params, fields)
            if not items:
                break
            if previous_first is not None and items[0] == previous_first:
                logger.warning(
                    "接口 %s 似乎不支持 offset 分页（新一页与上一页首行相同），停在 %d 行",
                    api_name,
                    len(rows),
                )
                break
            for item in items:
                if len(item) != len(field_names):
                    raise TushareError(
                        f"{api_name}: 行宽与字段数不一致（{len(item)} vs {len(field_names)}）",
                        api_name=api_name,
                    )
                rows.append(dict(zip(field_names, item, strict=True)))
            if len(items) < size:
                break
            previous_first = items[0]
            offset += size
        else:
            raise TushareError(
                f"{api_name}: 翻了 {MAX_PAGES} 页还没取完（已取 {len(rows)} 行），"
                f"说明查询区间太大。把区间切细再拉，别硬翻——代理的 offset 有上限，"
                f"硬翻下去只会撞上「参数校验失败」那种看不懂的报错",
                api_name=api_name,
            )
        return rows

    # ── 能力探测 ────────────────────────────────────────────────
    def probe(self, api_name: str, params: dict | None = None) -> tuple[bool, str]:
        """试调一次，判断这个接口当前账号能不能用。

        返回 (可用, 不可用的原因)。只有权限/积分不足才算"不可用"；
        网络错误、返回格式异常一律抛出，不静默降级成"没有这项数据"。
        """
        try:
            self.call_page(api_name, {**(params or {}), "limit": 1})
        except TushareAuthError as exc:
            return False, str(exc)
        return True, ""
