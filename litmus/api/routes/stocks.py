"""GET /api/stocks（找股票）、GET /api/stocks/{code}/kline（个股回看页的 K 线图，前复权）（ARCHITECTURE §6）。"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from litmus.api.serialize import to_jsonable
from litmus.api.services import services_of
from litmus.data import STOCK, MissingDataError

router = APIRouter()

_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

#: 找股票最多返回几个候选
SEARCH_LIMIT = 20


@router.get("/api/stocks")
def search_stocks(request: Request, q: str | None = None) -> dict[str, object]:
    """找股票：代码、名称、拼音首字母、曾用名、简称都行（规则见 ARCHITECTURE §2.3），按规则优先级排。
    total 是全部命中的只数，matches 最多给 SEARCH_LIMIT 个。"""
    text = (q or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="q 不能为空")
    matches = services_of(request).ds.resolve_stock(text)
    return {
        "query": text,
        "total": len(matches),
        "matches": [
            {
                "code": match.code,
                "name": match.name,
                "rule": match.rule_label,
                "matched": match.matched,
                "delisted": match.delisted,
            }
            for match in matches[:SEARCH_LIMIT]
        ],
    }


@router.get("/api/stocks/{code}/kline")
def get_kline(
    request: Request,
    code: str,
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
) -> dict[str, object]:
    """from、to 写成 YYYY-MM-DD，超出本地数据的部分自动裁掉。价格是前复权（基准日见 base_date），
    `*_raw` 是当天的真实成交价。参数写错返回 400，没有这只股票返回 404。"""
    start_day, end_day = _day(start, "from"), _day(end, "to")
    if start_day > end_day:
        raise HTTPException(status_code=400, detail=f"from {start_day} 晚于 to {end_day}")
    ds = services_of(request).ds
    first, last = ds.data_range(STOCK)
    start_day, end_day = max(start_day, first), min(end_day, last)
    if start_day > end_day:
        raise HTTPException(status_code=400, detail=f"本地股票数据只覆盖 {first} ~ {last}")
    info = ds.stock_info([code], end_day).row(0, named=True)
    if info["list_date"] is None:
        raise HTTPException(status_code=404, detail=f"本地没有股票代码 {code}")
    try:
        kline = ds.get_kline(code, start_day, end_day)
    except MissingDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "code": code,
        "name": info["name"],
        "adjust": "前复权",
        "base_date": to_jsonable(kline.base_date),
        "range": [start_day.isoformat(), end_day.isoformat()],
        "rows": to_jsonable(kline.rows.to_dicts()),
    }


def _day(value: str | None, name: str) -> date:
    if value is None or not _ISO_DAY.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"{name} 要写成 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} 不是有效日期：{value}") from exc
