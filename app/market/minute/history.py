"""Historical minute bar processing and multi-day 120m synthesis.

Pure standard library module for parsing EastMoney multi-day 5m KLine rows
and aggregating historical closed sessions into 120m bars.
No external networking, pandas, or numpy dependencies.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from app.chan.models import KLine

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _normalize_shanghai_aware(dt: Any, name: str) -> datetime:
    if not isinstance(dt, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(dt).__name__}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return dt.astimezone(SHANGHAI)


def _parse_finite_positive(val_str: Any, name: str) -> float:
    if isinstance(val_str, bool):
        raise TypeError(f"{name} must be a number, not bool")
    try:
        val = float(val_str)
    except Exception as exc:
        raise ValueError(f"{name} must be a valid float number: {val_str!r}") from exc
    if not math.isfinite(val):
        raise ValueError(f"{name} must be finite, got {val}")
    if val <= 0:
        raise ValueError(f"{name} must be positive, got {val}")
    return val


def _parse_finite_non_negative(val_str: Any, name: str) -> float:
    if isinstance(val_str, bool):
        raise TypeError(f"{name} must be a number, not bool")
    try:
        val = float(val_str)
    except Exception as exc:
        raise ValueError(f"{name} must be a valid float number: {val_str!r}") from exc
    if not math.isfinite(val):
        raise ValueError(f"{name} must be finite, got {val}")
    if val < 0:
        raise ValueError(f"{name} must be non-negative, got {val}")
    return val


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite non-bool number, got {value!r}")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite, got {converted}")
    return converted


def parse_history_rows(
    rows: Any,
    *,
    now: datetime | None = None,
) -> tuple[KLine, ...]:
    """Parse raw historical 5m rows from EastMoney push2his klt=5 into validated KLines.

    Row format: "YYYY-MM-DD HH:MM,open,close,high,low,volume[,...]"
    API column order: time (0), open (1), close (2), high (3), low (4), volume (5).

    Strict validations:
    - 5-minute alignment (minute % 5 == 0, 0 seconds/microseconds)
    - CN trading hours: 09:35-11:30 and 13:00-15:00
    - Strictly increasing timestamps (no duplicates, no backwards steps)
    - Timestamp <= now (if now is provided)
    - Finite positive prices, non-negative volume
    - high >= max(open, close), low <= min(open, close), high >= low
    """
    if rows is None:
        raise TypeError("rows cannot be None")
    if isinstance(rows, (str, bytes)):
        raise TypeError("rows must be an iterable of string rows, not a single string")
    if not isinstance(rows, Iterable):
        raise TypeError("rows must be an iterable")

    shanghai_now: datetime | None = None
    if now is not None:
        shanghai_now = _normalize_shanghai_aware(now, "now")

    result: list[KLine] = []
    prev_dt: datetime | None = None

    for row in rows:
        if not isinstance(row, str):
            raise TypeError(f"each row must be a string, got {type(row).__name__}")
        parts = [p.strip() for p in row.split(",")]
        if len(parts) < 6:
            raise ValueError(f"row has insufficient columns (expected >= 6, got {len(parts)}): {row!r}")

        time_str = parts[0]
        try:
            dt_naive = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        except Exception as exc:
            raise ValueError(f"invalid time format {time_str!r}, expected 'YYYY-MM-DD HH:MM'") from exc

        if dt_naive.second != 0 or dt_naive.microsecond != 0 or dt_naive.minute % 5 != 0:
            raise ValueError(f"timestamp {time_str} must align to a 5-minute close boundary")

        dt_aware = dt_naive.replace(tzinfo=SHANGHAI)

        t = dt_aware.time()
        is_morning = time(9, 35) <= t <= time(11, 30)
        is_afternoon = time(13, 0) <= t <= time(15, 0)
        if not (is_morning or is_afternoon):
            raise ValueError(f"timestamp {time_str} is outside CN trading hours")

        if prev_dt is not None and dt_aware <= prev_dt:
            raise ValueError(f"timestamps must be strictly increasing: {dt_aware} <= {prev_dt}")
        prev_dt = dt_aware

        if shanghai_now is not None and dt_aware > shanghai_now:
            raise ValueError(f"future timestamp {dt_aware} exceeds now {shanghai_now}")

        o = _parse_finite_positive(parts[1], "open")
        c = _parse_finite_positive(parts[2], "close")
        h = _parse_finite_positive(parts[3], "high")
        l = _parse_finite_positive(parts[4], "low")
        v = _parse_finite_non_negative(parts[5], "volume")

        if h < max(o, c):
            raise ValueError(f"high ({h}) must bound open ({o}) and close ({c})")
        if l > min(o, c):
            raise ValueError(f"low ({l}) must bound open ({o}) and close ({c})")
        if h < l:
            raise ValueError(f"high ({h}) must be >= low ({l})")

        result.append(
            KLine(
                time=dt_aware.isoformat(),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=v,
            )
        )

    return tuple(result)


def build_history_120m(
    lines: Sequence[KLine] | tuple[KLine, ...] | list[KLine],
    *,
    now: datetime,
) -> tuple[KLine, ...]:
    """Aggregate multi-day 5m historical KLines into 120m bars for closed trading days.

    Grouping:
    - Group by (date, session) where session is 'morning' (09:30-11:30) or 'afternoon' (13:00-15:00).
    - Morning bucket closes at 11:30:00+08:00.
    - Afternoon bucket closes at 15:00:00+08:00.
    - Each 120m bucket must contain exactly 24 5m bars; incomplete buckets are discarded.
    - Bars belonging to the current day (date == now.date()) are NEVER aggregated here (handled by daily resample).
    - Any line with time > now triggers an error.
    - Returns tuple of synthesized KLines with ISO timestamps strictly increasing across days.
    """
    shanghai_now = _normalize_shanghai_aware(now, "now")
    today = shanghai_now.date()

    if not isinstance(lines, (tuple, list)):
        raise TypeError(f"lines must be a tuple or list of KLine, got {type(lines).__name__}")
    if not lines:
        return ()

    sessions_data: dict[tuple[date, str], list[KLine]] = {}
    dates_seen: list[date] = []
    prev_dt: datetime | None = None

    for line in lines:
        if not isinstance(line, KLine):
            raise TypeError(f"each item in lines must be a KLine, got {type(line).__name__}")

        t = line.time
        if isinstance(t, str):
            try:
                t_dt = datetime.fromisoformat(t)
            except Exception as exc:
                raise ValueError(f"invalid KLine ISO time {t!r}") from exc
        elif isinstance(t, datetime):
            t_dt = t
        else:
            raise TypeError("KLine time must be an ISO string or datetime")

        if t_dt.tzinfo is None or t_dt.utcoffset() is None:
            raise ValueError("KLine time must be timezone-aware")
        t_shanghai = t_dt.astimezone(SHANGHAI)

        if t_shanghai > shanghai_now:
            raise ValueError(f"KLine time {t_shanghai} exceeds current time {shanghai_now}")

        if prev_dt is not None and t_shanghai <= prev_dt:
            raise ValueError(f"lines must be strictly increasing: {t_shanghai} <= {prev_dt}")
        prev_dt = t_shanghai

        o = _finite_number(line.open, "open")
        h = _finite_number(line.high, "high")
        l = _finite_number(line.low, "low")
        c = _finite_number(line.close, "close")
        v = _finite_number(line.volume, "volume")
        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            raise ValueError("OHLC prices must be positive")
        if v < 0:
            raise ValueError("volume must be non-negative")
        if h < max(o, c) or l > min(o, c) or h < l:
            raise ValueError("high/low bounds violated")

        line_date = t_shanghai.date()
        # Current day bars are excluded from historical 120m aggregation
        if line_date >= today:
            continue

        time_of_day = t_shanghai.time()
        if time(9, 35) <= time_of_day <= time(11, 30):
            session_name = "morning"
        elif time(13, 0) <= time_of_day <= time(15, 0):
            session_name = "afternoon"
        else:
            raise ValueError(f"timestamp {t_shanghai} is outside CN trading hours")

        if line_date not in dates_seen:
            dates_seen.append(line_date)

        key = (line_date, session_name)
        if key not in sessions_data:
            sessions_data[key] = []
        sessions_data[key].append(line)

    result_120m: list[KLine] = []
    for d in dates_seen:
        morning_bars = sessions_data.get((d, "morning"), [])
        if len(morning_bars) == 24:
            bucket_close = datetime.combine(d, time(11, 30), tzinfo=SHANGHAI)
            result_120m.append(
                KLine(
                    time=bucket_close.isoformat(),
                    open=float(morning_bars[0].open),
                    high=max(float(b.high) for b in morning_bars),
                    low=min(float(b.low) for b in morning_bars),
                    close=float(morning_bars[-1].close),
                    volume=sum(float(b.volume) for b in morning_bars),
                )
            )

        afternoon_bars = sessions_data.get((d, "afternoon"), [])
        if len(afternoon_bars) == 24:
            bucket_close = datetime.combine(d, time(15, 0), tzinfo=SHANGHAI)
            result_120m.append(
                KLine(
                    time=bucket_close.isoformat(),
                    open=float(afternoon_bars[0].open),
                    high=max(float(b.high) for b in afternoon_bars),
                    low=min(float(b.low) for b in afternoon_bars),
                    close=float(afternoon_bars[-1].close),
                    volume=sum(float(b.volume) for b in afternoon_bars),
                )
            )

    return tuple(result_120m)


__all__ = [
    "SHANGHAI",
    "parse_history_rows",
    "build_history_120m",
]
