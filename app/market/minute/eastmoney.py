from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
from typing import Any
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.market.board_http import _parse_finite_float
from app.market.global_markets import (
    ERROR_MALFORMED,
    ERROR_SOURCE_ERROR,
    GlobalMarketDataError,
    _BoundedHttpProvider,
    TIMEOUT_SECONDS,
)
from app.market.minute.context import MinuteSnapshot
from app.market.minute.session import cn_trading_day, TradingDay, SessionWindow

SHANGHAI = ZoneInfo("Asia/Shanghai")
_KLINE_BASE_URL = "https://push2delay.eastmoney.com/api/qt/stock/kline/get"


def _secid(code: str) -> str:
    """Map a 6-digit CN stock or fund code to EastMoney secid prefix.

    Prefix 1. for Shanghai (first digit 5, 6, 9).
    Prefix 0. for Shenzhen (first digit 0, 1, 2, 3).
    Non-6-digit, non-numeric or unsupported prefixes raise ValueError.
    """
    if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
        raise ValueError(f"Stock code must be a 6-digit numeric string, got {code!r}")
    first = code[0]
    if first in ("5", "6", "9"):
        return f"1.{code}"
    if first in ("0", "1", "2", "3"):
        return f"0.{code}"
    raise ValueError(f"Unsupported market prefix for code: {code!r}")


def _normalize_row(item: Any) -> tuple[datetime, float, float, float, float, float]:
    """Normalize input row to (dt_aware, open, high, low, close, volume)."""
    if isinstance(item, str):
        parts = item.split(",")
        if len(parts) < 6:
            raise GlobalMarketDataError(ERROR_MALFORMED)
        try:
            dt_naive = datetime.strptime(parts[0].strip(), "%Y-%m-%d %H:%M")
        except Exception:
            raise GlobalMarketDataError(ERROR_MALFORMED)
        dt_aware = dt_naive.replace(tzinfo=SHANGHAI)
        o = _parse_finite_float(parts[1])
        c = _parse_finite_float(parts[2])
        h = _parse_finite_float(parts[3])
        l = _parse_finite_float(parts[4])
        v = _parse_finite_float(parts[5])
        if (
            o is None or c is None or h is None or l is None or v is None
            or o <= 0 or c <= 0 or h <= 0 or l <= 0 or v < 0
            or h < max(o, c, l) or l > min(o, c, h)
        ):
            raise GlobalMarketDataError(ERROR_MALFORMED)
        return (dt_aware, o, h, l, c, v)
    elif hasattr(item, "time") and hasattr(item, "open"):
        t = item.time
        if isinstance(t, str):
            t = datetime.fromisoformat(t)
        if not isinstance(t, datetime) or t.tzinfo is None or t.utcoffset() is None:
            raise ValueError("KLine time must be timezone-aware")
        dt_aware = t.astimezone(SHANGHAI)
        o = _parse_finite_float(item.open)
        h = _parse_finite_float(item.high)
        l = _parse_finite_float(item.low)
        c = _parse_finite_float(item.close)
        v = _parse_finite_float(item.volume)
        if (
            o is None or c is None or h is None or l is None or v is None
            or o <= 0 or c <= 0 or h <= 0 or l <= 0 or v < 0
            or h < max(o, c, l) or l > min(o, c, h)
        ):
            raise GlobalMarketDataError(ERROR_MALFORMED)
        return (dt_aware, o, h, l, c, v)
    elif isinstance(item, (tuple, list)):
        if len(item) < 6:
            raise GlobalMarketDataError(ERROR_MALFORMED)
        t = item[0]
        if isinstance(t, str):
            t = datetime.fromisoformat(t)
        if not isinstance(t, datetime) or t.tzinfo is None or t.utcoffset() is None:
            raise ValueError("time must be timezone-aware")
        dt_aware = t.astimezone(SHANGHAI)
        o = _parse_finite_float(item[1])
        h = _parse_finite_float(item[2])
        l = _parse_finite_float(item[3])
        c = _parse_finite_float(item[4])
        v = _parse_finite_float(item[5])
        if (
            o is None or c is None or h is None or l is None or v is None
            or o <= 0 or c <= 0 or h <= 0 or l <= 0 or v < 0
            or h < max(o, c, l) or l > min(o, c, h)
        ):
            raise GlobalMarketDataError(ERROR_MALFORMED)
        return (dt_aware, o, h, l, c, v)
    else:
        raise GlobalMarketDataError(ERROR_MALFORMED)


def aggregate_to_5m(
    rows: Any,
    *,
    day: TradingDay,
    now: datetime | None = None,
) -> tuple[KLine, ...]:
    """Aggregate 1-minute rows into 5-minute right-closed KLines.

    - day: TradingDay instance.
    - Each session generates 5m right-closed grid closes:
      session.opens_at + 5m, +10m, ..., closes_at.
    - Each 1m row is placed into the smallest grid close >= row.time.
    - Incomplete buckets (< 5 1-minute bars) are discarded whole.
    - Output KLine time is the grid close in ISO format (+08:00).
    - OHLCV: open=first open, high=max high, low=min low, close=last close, volume=sum volume.
    - Returns strictly ascending tuple of KLine.
    - Malformed/future/out-of-session/out-of-order data raises GlobalMarketDataError(ERROR_MALFORMED).
    """
    if not isinstance(day, TradingDay):
        raise TypeError("day must be a TradingDay")

    if not rows or not day.sessions:
        return ()

    if not isinstance(rows, (list, tuple)):
        raise GlobalMarketDataError(ERROR_MALFORMED)

    if now is not None:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        now_shanghai = now.astimezone(SHANGHAI)
    else:
        now_shanghai = None

    session_buckets: list[tuple[SessionWindow, list[datetime]]] = []
    bucket_map: dict[datetime, list[tuple[datetime, float, float, float, float, float]]] = {}
    ordered_closes: list[datetime] = []

    for session in day.sessions:
        total_minutes = int((session.closes_at - session.opens_at).total_seconds() // 60)
        closes = [
            session.opens_at + timedelta(minutes=m)
            for m in range(5, total_minutes + 1, 5)
        ]
        session_buckets.append((session, closes))
        for c in closes:
            ordered_closes.append(c)
            bucket_map[c] = []

    prev_dt: datetime | None = None

    for item in rows:
        norm_row = _normalize_row(item)
        row_dt = norm_row[0]

        if row_dt.date() != day.market_date:
            raise GlobalMarketDataError(ERROR_MALFORMED)

        if now_shanghai is not None and row_dt > now_shanghai:
            raise GlobalMarketDataError(ERROR_MALFORMED)

        if prev_dt is not None and row_dt <= prev_dt:
            raise GlobalMarketDataError(ERROR_MALFORMED)
        prev_dt = row_dt

        target_close: datetime | None = None
        for session, closes in session_buckets:
            if session.opens_at < row_dt <= session.closes_at:
                for c in closes:
                    if c >= row_dt:
                        target_close = c
                        break
                break

        if target_close is None:
            raise GlobalMarketDataError(ERROR_MALFORMED)

        bucket_map[target_close].append(norm_row)

    result: list[KLine] = []
    for close_dt in ordered_closes:
        b_rows = bucket_map[close_dt]
        if len(b_rows) < 5:
            continue

        b_rows.sort(key=lambda r: r[0])
        open_val = b_rows[0][1]
        high_val = max(r[2] for r in b_rows)
        low_val = min(r[3] for r in b_rows)
        close_val = b_rows[-1][4]
        volume_val = sum(r[5] for r in b_rows)

        result.append(
            KLine(
                time=close_dt.isoformat(),
                open=open_val,
                high=high_val,
                low=low_val,
                close=close_val,
                volume=volume_val,
            )
        )

    return tuple(result)


class EastMoneyMinuteProvider(_BoundedHttpProvider):
    """EastMoney 1-minute klines HTTP provider bounded by transport constraints."""

    REQUEST_HEADERS = {
        "Accept": "application/json",
        "User-Agent": "investment-assistant-public-trial/1.0",
        "Referer": "https://quote.eastmoney.com/",
    }

    def __init__(self, session=None, timeout=TIMEOUT_SECONDS):
        super().__init__(session=session, timeout=timeout)

    def fetch_rows(self, code: str, *, now: datetime) -> list[str]:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware datetime")
        shanghai_now = now.astimezone(SHANGHAI)

        secid = _secid(code)
        params = (
            f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
            f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            f"&klt=1&fqt=1&beg=0&end=20500101&lmt=260"
        )
        url = f"{_KLINE_BASE_URL}?{params}"

        try:
            raw_bytes = self._get(url, None)
        except GlobalMarketDataError as err:
            raise GlobalMarketDataError(ERROR_SOURCE_ERROR) from err

        try:
            text = raw_bytes.decode("utf-8")
            payload = json.loads(text)
        except Exception:
            raise GlobalMarketDataError(ERROR_MALFORMED) from None

        if not isinstance(payload, dict):
            raise GlobalMarketDataError(ERROR_MALFORMED)

        if payload.get("rc") != 0:
            raise GlobalMarketDataError(ERROR_MALFORMED)

        data = payload.get("data")
        if data is None:
            return []
        if not isinstance(data, dict):
            raise GlobalMarketDataError(ERROR_MALFORMED)

        klines = data.get("klines")
        if not klines:
            return []
        if not isinstance(klines, list):
            raise GlobalMarketDataError(ERROR_MALFORMED)

        return klines

    def snapshot(self, holding: Any, *, now: datetime) -> MinuteSnapshot:
        if getattr(holding, "market", None) != "CN" or getattr(holding, "valuation_mode", None) != "exchange":
            raise ValueError("EastMoney minute source only supports CN exchange holdings")

        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware datetime")
        shanghai_now = now.astimezone(SHANGHAI)

        code = getattr(holding, "code", "")
        raw_klines = self.fetch_rows(code, now=shanghai_now)

        day = cn_trading_day(
            shanghai_now.date(),
            is_open=bool(raw_klines),
            source="eastmoney push2delay klt=1 presence",
        )

        lines = aggregate_to_5m(raw_klines, day=day, now=shanghai_now)

        return MinuteSnapshot(
            code=code,
            market="CN",
            day=day,
            lines=lines,
            source="eastmoney:push2delay:klt=1->5m",
            fetched_at=shanghai_now,
            base_minutes=5,
            timestamp_semantics="close",
        )


EastMoneyMinuteAdapter = EastMoneyMinuteProvider
EastMoneyMinuteSource = EastMoneyMinuteProvider


def create_minute_snapshot_loader(session=None):
    """Factory returning a minute snapshot loader function without global side effects."""
    provider = EastMoneyMinuteProvider(session=session)

    def loader(holding: Any, *, now: datetime) -> MinuteSnapshot:
        return provider.snapshot(holding, now=now)

    return loader
