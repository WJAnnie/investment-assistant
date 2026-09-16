# -*- coding: utf-8 -*-
"""Pandas-free, akshare-free EastMoney industry board HTTP client.

Designed for public trial environments where pandas and akshare are not available.
Implements duck-typed methods matching AkShare's industry board interface.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import re
from types import MappingProxyType
from typing import Any

from app.market.global_markets import (
    ERROR_MALFORMED,
    GlobalMarketDataError,
    _BoundedHttpProvider,
)

# push2delay.eastmoney.com is used because push2.eastmoney.com returns HTTP 302
# under the inherited non-following transport (allow_redirects=False).
_LIST_BASE_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
_MAX_PAGES = 8
_KLINE_BASE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

_DATE_HYPHEN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_COMPACT_RE = re.compile(r"^\d{8}$")


class RecordList(list):
    """Duck-typing DataFrame-compatible list for AkShareIndustryProvider."""

    def to_dict(self, orient: str = "records") -> list[dict[str, Any]]:
        if orient == "records":
            return list(self)
        return list(self)


def _parse_finite_float(val: Any) -> float | None:
    if val is None or type(val) is bool:
        return None
    if isinstance(val, str):
        s = val.strip()
        if not s or s == "-":
            return None
        try:
            val = float(s)
        except (ValueError, OverflowError):
            return None
    elif isinstance(val, (int, float)):
        val = float(val)
    else:
        return None
    if not math.isfinite(val):
        return None
    return val


def _parse_integer_count(val: Any) -> int | None:
    if val is None or type(val) is bool:
        return None
    if isinstance(val, str):
        s = val.strip()
        if not s or s == "-":
            return None
        try:
            f = float(s)
        except (ValueError, OverflowError):
            return None
    elif isinstance(val, (int, float)):
        f = float(val)
    else:
        return None
    if not math.isfinite(f) or not f.is_integer():
        return None
    return int(f)


def _normalize_date(val: Any) -> str | None:
    if val is None or type(val) is bool:
        return None
    if isinstance(val, str):
        s = val.strip()
        if _DATE_HYPHEN_RE.match(s):
            try:
                datetime.strptime(s, "%Y-%m-%d")
                return s
            except ValueError:
                return None
        if _DATE_COMPACT_RE.match(s):
            try:
                d = datetime.strptime(s, "%Y%m%d")
                return d.strftime("%Y-%m-%d")
            except ValueError:
                return None
    return None


def _decode_json(raw_bytes: bytes) -> dict[str, Any]:
    try:
        text = raw_bytes.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, Exception):
        raise GlobalMarketDataError(ERROR_MALFORMED) from None
    if not isinstance(payload, dict):
        raise GlobalMarketDataError(ERROR_MALFORMED)
    return payload


class EastMoneyBoardClient(_BoundedHttpProvider):
    """Pandas-free HTTP client for EastMoney industry board endpoints."""

    REQUEST_HEADERS = MappingProxyType({
        "Accept": "application/json",
        "User-Agent": "investment-assistant-public-trial/1.0",
        "Referer": "https://quote.eastmoney.com/",
    })

    def __init__(self, session: Any = None, timeout: int | float = 10) -> None:
        super().__init__(session=session, timeout=timeout)

    def stock_board_industry_name_em(self) -> RecordList:
        records: list[dict[str, Any]] = []
        seen_codes: set[str] = set()

        for page in range(1, _MAX_PAGES + 1):
            url = (
                f"{_LIST_BASE_URL}?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2"
                f"&fid=f3&fs=m:90+t:2&fields=f12,f14,f3,f8,f104,f105,f109,f110"
            )
            raw_bytes = self._get(url, None)
            payload = _decode_json(raw_bytes)
            if "data" not in payload:
                raise GlobalMarketDataError(ERROR_MALFORMED)
            data = payload["data"]
            if data is None:
                break
            if not isinstance(data, dict):
                raise GlobalMarketDataError(ERROR_MALFORMED)

            diff = data.get("diff")
            if diff is None:
                break
            if isinstance(diff, list):
                if not diff:
                    break
                raw_rows = diff
            elif isinstance(diff, dict):
                if not diff:
                    break
                raw_rows = list(diff.values())
            else:
                raise GlobalMarketDataError(ERROR_MALFORMED)

            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                raw_code = row.get("f12")
                if raw_code is None or type(raw_code) is bool:
                    continue
                code = str(raw_code).strip()
                if not code or code in seen_codes:
                    continue

                raw_name = row.get("f14")
                if raw_name is None or type(raw_name) is bool:
                    continue
                name = str(raw_name).strip()
                if not name:
                    continue

                ret_pct = _parse_finite_float(row.get("f3"))
                if ret_pct is None:
                    continue

                turnover = _parse_finite_float(row.get("f8"))
                if turnover is None:
                    continue

                advancers = _parse_integer_count(row.get("f104"))
                if advancers is None:
                    continue

                decliners = _parse_integer_count(row.get("f105"))
                if decliners is None:
                    continue

                ret_5d = _parse_finite_float(row.get("f109"))
                if ret_5d is None:
                    continue

                ret_20d = _parse_finite_float(row.get("f110"))
                if ret_20d is None:
                    continue

                seen_codes.add(code)
                records.append({
                    "板块代码": code,
                    "板块名称": name,
                    "涨跌幅": ret_pct,
                    "换手率": turnover,
                    "上涨家数": advancers,
                    "下跌家数": decliners,
                    "近5日涨跌幅": ret_5d,
                    "近20日涨跌幅": ret_20d,
                })

        return RecordList(records)

    def stock_board_industry_hist_em(
        self,
        symbol: str,
        start_date: str = "",
        end_date: str = "",
        period: str = "日k",
        adjust: str = "",
    ) -> RecordList:
        secid = f"90.{symbol}"
        kline_url = (
            f"{_KLINE_BASE_URL}?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
            f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            f"&klt=101&fqt=0&beg={start_date}&end={end_date}"
        )
        raw_bytes = self._get(kline_url, None)
        payload = _decode_json(raw_bytes)
        data = payload.get("data")
        if data is None:
            return RecordList()
        if not isinstance(data, dict):
            raise GlobalMarketDataError(ERROR_MALFORMED)
        klines = data.get("klines")
        if not klines:
            return RecordList()
        if not isinstance(klines, list):
            raise GlobalMarketDataError(ERROR_MALFORMED)

        records: list[dict[str, Any]] = []
        for item in klines:
            if not isinstance(item, str):
                continue
            parts = item.split(",")
            if len(parts) < 3:
                continue
            norm_date = _normalize_date(parts[0])
            if norm_date is None:
                continue
            close = _parse_finite_float(parts[2])
            if close is None:
                continue
            records.append({
                "日期": norm_date,
                "收盘": close,
            })

        return RecordList(records)
