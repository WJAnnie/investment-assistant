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
from zoneinfo import ZoneInfo

from app.market.global_markets import (
    ERROR_MALFORMED,
    GlobalMarketDataError,
    _BoundedHttpProvider,
)

# push2delay.eastmoney.com is used because push2.eastmoney.com returns HTTP 302
# under the inherited non-following transport (allow_redirects=False).
_LIST_BASE_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
_MAX_PAGES = 8
_MAX_SWEEPS = 4
_KLINE_BASE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

_DATE_HYPHEN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_COMPACT_RE = re.compile(r"^\d{8}$")
_EPOCH_DIGIT_RE = re.compile(r"^\d+$")

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


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


def _parse_epoch_seconds(val: Any) -> int | None:
    if val is None or type(val) is bool:
        return None
    try:
        if isinstance(val, str):
            s = val.strip()
            if not _EPOCH_DIGIT_RE.match(s):
                return None
            ts = int(s)
        elif isinstance(val, int):
            ts = val
        elif isinstance(val, float):
            if not math.isfinite(val) or not val.is_integer():
                return None
            if not (1_000_000_000 <= val <= 4_000_000_000):
                return None
            ts = int(val)
        else:
            return None
        if not (1_000_000_000 <= ts <= 4_000_000_000):
            return None
        return ts
    except (ValueError, OverflowError, TypeError):
        return None


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

    def __init__(
        self,
        session: Any = None,
        timeout: int | float = 10,
        max_pages: int | None = None,
        max_sweeps: int | None = None,
    ) -> None:
        super().__init__(session=session, timeout=timeout)
        self._max_pages = max_pages
        self._max_sweeps = max_sweeps

    def _fetch_page(self, page: int) -> list[dict[str, Any]] | None:
        url = (
            f"{_LIST_BASE_URL}?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2"
            f"&fid=f3&fs=m:90+t:2&fields=f12,f14,f3,f8,f104,f105,f109,f110,f124"
        )
        raw_bytes = self._get(url, None)
        payload = _decode_json(raw_bytes)
        if "data" not in payload:
            raise GlobalMarketDataError(ERROR_MALFORMED)
        data = payload["data"]
        if data is None:
            return None
        if not isinstance(data, dict):
            raise GlobalMarketDataError(ERROR_MALFORMED)

        diff = data.get("diff")
        if diff is None:
            return None
        if isinstance(diff, list):
            if not diff:
                return None
            raw_rows = diff
        elif isinstance(diff, dict):
            if not diff:
                return None
            raw_rows = list(diff.values())
        else:
            raise GlobalMarketDataError(ERROR_MALFORMED)

        page_records: list[dict[str, Any]] = []
        page_seen: set[str] = set()
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            raw_code = row.get("f12")
            if raw_code is None or type(raw_code) is bool:
                continue
            code = str(raw_code).strip()
            if not code or code in page_seen:
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

            data_time = _parse_epoch_seconds(row.get("f124"))
            if data_time is None:
                continue

            try:
                iso_time = datetime.fromtimestamp(data_time, SHANGHAI_TZ).isoformat()
            except (OverflowError, OSError, ValueError):
                continue

            page_seen.add(code)
            page_records.append({
                "板块代码": code,
                "板块名称": name,
                "涨跌幅": ret_pct,
                "换手率": turnover,
                "上涨家数": advancers,
                "下跌家数": decliners,
                "近5日涨跌幅": ret_5d,
                "近20日涨跌幅": ret_20d,
                "数据时间": iso_time,
            })

        return page_records

    def stock_board_industry_name_em(self) -> RecordList:
        max_pages = self._max_pages if self._max_pages is not None else _MAX_PAGES
        max_sweeps = self._max_sweeps if self._max_sweeps is not None else _MAX_SWEEPS

        pages_records: dict[int, list[dict[str, Any]]] = {}

        # Sweep 1: 分页抓取，直到 diff 为空或达到 max_pages
        for page in range(1, max_pages + 1):
            page_rows = self._fetch_page(page)
            if page_rows is None:
                break
            pages_records[page] = page_rows

        for sweep in range(1, max_sweeps + 1):
            all_rows = [row for p in sorted(pages_records.keys()) for row in pages_records[p]]
            if not all_rows:
                return RecordList()

            # 计算全量行主导戳：行数最多为准，并列取时间较早者
            counts: dict[str, int] = {}
            for row in all_rows:
                ts = row["数据时间"]
                counts[ts] = counts.get(ts, 0) + 1

            dominant_ts = min(
                counts.keys(),
                key=lambda t: (-counts[t], datetime.fromisoformat(t)),
            )

            # 判定哪些页不收敛于主导戳
            divergent_pages = [
                p
                for p in sorted(pages_records.keys())
                if {r["数据时间"] for r in pages_records[p]} != {dominant_ts}
            ]

            if not divergent_pages:
                # 收敛：按既有去重规则返回（先到先得）
                seen_codes: set[str] = set()
                records: list[dict[str, Any]] = []
                for p in sorted(pages_records.keys()):
                    for row in pages_records[p]:
                        code = row["板块代码"]
                        if code not in seen_codes:
                            seen_codes.add(code)
                            records.append(row)
                return RecordList(records)

            if sweep == max_sweeps:
                break

            # 仅重抓戳集合 != {主导戳} 的页（整页替换，不追加、不去重累积）
            for p in divergent_pages:
                new_rows = self._fetch_page(p)
                # 只有拿到「非空」新数据才整页替换；重抓失败/空响应时保留首轮已抓到的行，
                # 使该页继续保持「与主导戳不一致」，从而在本轮/下一轮继续重抓；
                # 若直到上限仍不收敛，则走下方并集分支，交由 provider 照旧整批判 DATE_MISMATCH（诚实失败）。
                if new_rows:
                    pages_records[p] = new_rows

        # 达到上限仍不收敛：返回已抓取全部行的并集（按 板块代码 去重、后者覆盖前者）
        dedup_records: dict[str, dict[str, Any]] = {}
        for p in sorted(pages_records.keys()):
            for row in pages_records[p]:
                dedup_records[row["板块代码"]] = row
        return RecordList(list(dedup_records.values()))

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
