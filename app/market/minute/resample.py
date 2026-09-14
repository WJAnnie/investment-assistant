"""Strict 5-minute source validation and in-session resampling.

The resampler treats source K lines as close/right stamped 5-minute bars for
the interval ``(open, close]``. It validates the whole input before producing
any aggregate evidence and only exposes aggregate OHLCV through CLOSED bars.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.chan.models import KLine
from app.domain import BarStatus, Timeframe
from app.market.minute.session import BASE_MINUTES, SHANGHAI, SessionWindow, TradingDay


_TARGET_MINUTES = {
    Timeframe.MIN_5: 5,
    Timeframe.MIN_15: 15,
    Timeframe.MIN_30: 30,
    Timeframe.MIN_120: 120,
}


@dataclass(frozen=True)
class ResampledBar:
    timeframe: Timeframe
    session: str
    opens_at: datetime
    expected_close: datetime
    status: BarStatus
    line: KLine | None
    observed: int
    expected: int
    reason: str


def _normalize_now(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(SHANGHAI)


def _parse_time(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        raise TypeError("KLine time must be an aware datetime or ISO string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("KLine time must include timezone offset")
    return parsed.astimezone(SHANGHAI)


def _finite_number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite non-bool number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def _validate_price_line(line: KLine) -> tuple[float, float, float, float, float]:
    open_ = _finite_number(line.open, "open")
    high = _finite_number(line.high, "high")
    low = _finite_number(line.low, "low")
    close = _finite_number(line.close, "close")
    volume = _finite_number(line.volume, "volume")
    if open_ <= 0 or high <= 0 or low <= 0 or close <= 0:
        raise ValueError("OHLC values must be positive")
    if volume < 0:
        raise ValueError("volume must be non-negative")
    if high < max(open_, low, close):
        raise ValueError("high must bound open/low/close")
    if low > min(open_, high, close):
        raise ValueError("low must bound open/high/close")
    return open_, high, low, close, volume


def _bucket_contains_session_time(session: SessionWindow, timestamp: datetime) -> bool:
    return session.opens_at < timestamp <= session.closes_at


def _session_for_timestamp(day: TradingDay, timestamp: datetime) -> SessionWindow | None:
    for session in day.sessions:
        if _bucket_contains_session_time(session, timestamp):
            return session
    return None


def _validate_lines(lines, *, day: TradingDay, now: datetime) -> dict[datetime, KLine]:
    if not isinstance(day, TradingDay):
        raise TypeError("day must be a TradingDay")
    if not isinstance(lines, (list, tuple)):
        raise TypeError("lines must be a list or tuple of KLine values")

    normalized: dict[datetime, KLine] = {}
    previous: datetime | None = None
    for line in lines:
        if not isinstance(line, KLine):
            raise TypeError("lines must contain only KLine values")
        timestamp = _parse_time(line.time)
        if timestamp.date() != day.market_date:
            raise ValueError("KLine timestamp must match trading day")
        if timestamp > now:
            raise ValueError("future KLine timestamp is not allowed")
        if previous is not None and timestamp <= previous:
            raise ValueError("KLine timestamps must be strictly increasing")
        if timestamp.second or timestamp.microsecond or timestamp.minute % BASE_MINUTES != 0:
            raise ValueError("KLine timestamp must align to a 5-minute close boundary")
        if _session_for_timestamp(day, timestamp) is None:
            raise ValueError("KLine timestamp is outside trading sessions")
        _validate_price_line(line)
        normalized[timestamp] = line
        previous = timestamp
    return normalized


def _slot_closes(session: SessionWindow) -> tuple[datetime, ...]:
    closes = []
    value = session.opens_at + timedelta(minutes=BASE_MINUTES)
    while value <= session.closes_at:
        closes.append(value)
        value += timedelta(minutes=BASE_MINUTES)
    return tuple(closes)


def _make_line(closes: tuple[datetime, ...], source: dict[datetime, KLine]) -> KLine:
    first = source[closes[0]]
    last = source[closes[-1]]
    high = max(float(source[close].high) for close in closes)
    low = min(float(source[close].low) for close in closes)
    volume = sum(float(source[close].volume) for close in closes)
    values = (float(first.open), high, low, float(last.close), volume)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("aggregate OHLCV values must be finite")
    return KLine(closes[-1].isoformat(), values[0], values[1], values[2], values[3], values[4])


def _reason(status: BarStatus, *, observed: int, expected: int, now: datetime, expected_close: datetime) -> str:
    if status is BarStatus.CLOSED:
        return "closed"
    if status is BarStatus.FORMING:
        return "forming"
    if status is BarStatus.INVALID:
        return "short tail cannot form complete target timeframe"
    if now < expected_close:
        return f"missing due 5m bars ({observed}/{expected})"
    return f"missing required 5m bars ({observed}/{expected})"


def _status_for_bucket(
    closes: tuple[datetime, ...],
    *,
    opens_at: datetime,
    expected: int,
    source: dict[datetime, KLine],
    now: datetime,
    invalid: bool,
) -> tuple[BarStatus, int]:
    observed = sum(1 for close in closes if close in source)
    expected_close = closes[-1]
    if invalid:
        return BarStatus.INVALID, observed
    if now < opens_at:
        return BarStatus.MISSING, observed
    if now >= expected_close:
        if observed == expected:
            return BarStatus.CLOSED, observed
        return BarStatus.MISSING, observed

    due = tuple(close for close in closes if close <= now)
    due_observed = sum(1 for close in due if close in source)
    if due_observed == len(due):
        return BarStatus.FORMING, observed
    return BarStatus.MISSING, observed


def _resample_session(
    session: SessionWindow,
    *,
    timeframe: Timeframe,
    target_minutes: int,
    source: dict[datetime, KLine],
    now: datetime,
) -> tuple[ResampledBar, ...]:
    session_closes = _slot_closes(session)
    expected = target_minutes // BASE_MINUTES
    bars = []
    for index in range(0, len(session_closes), expected):
        closes = session_closes[index : index + expected]
        if not closes:
            continue
        invalid = len(closes) < expected
        opens_at = closes[0] - timedelta(minutes=BASE_MINUTES)
        expected_close = closes[-1]
        status, observed = _status_for_bucket(
            closes,
            opens_at=opens_at,
            expected=expected,
            source=source,
            now=now,
            invalid=invalid,
        )
        line = _make_line(closes, source) if status is BarStatus.CLOSED else None
        bars.append(
            ResampledBar(
                timeframe,
                session.name,
                opens_at,
                expected_close,
                status,
                line,
                observed,
                expected,
                _reason(status, observed=observed, expected=expected, now=now, expected_close=expected_close),
            )
        )
    return tuple(bars)


def resample_minutes(lines, *, day: TradingDay, now: datetime) -> dict[Timeframe, tuple[ResampledBar, ...]]:
    """Resample ordered 5-minute KLine input into supported minute cycles.

    No sorting, filling, skipping, or cross-session merging is performed. Bad
    input rejects the entire list so downstream signal code cannot consume a
    partially sanitized history.
    """
    normalized_now = _normalize_now(now)
    source = _validate_lines(lines, day=day, now=normalized_now)
    result: dict[Timeframe, tuple[ResampledBar, ...]] = {}
    for timeframe, target_minutes in _TARGET_MINUTES.items():
        bars = []
        for session in day.sessions:
            bars.extend(
                _resample_session(
                    session,
                    timeframe=timeframe,
                    target_minutes=target_minutes,
                    source=source,
                    now=normalized_now,
                )
            )
        result[timeframe] = tuple(bars)
    return result


def current_bar(bars, *, now: datetime) -> ResampledBar | None:
    """Return the bar that is current at ``now`` without backdating evidence.

    CLOSED bars are valid only at or after their own ``expected_close``. Passing
    bars resampled at a later cutoff and selecting them for an earlier ``now``
    raises ``ValueError`` instead of relabeling future OHLC as current evidence.
    At touching session boundaries, a new session's opening bar takes precedence
    over the previous session's exact close; ordinary intra-session exact closes
    still select the just-closed bar.
    """
    normalized_now = _normalize_now(now)
    if not isinstance(bars, (list, tuple)):
        raise TypeError("bars must be a list or tuple")
    normalized_bars = []
    for bar in bars:
        if not isinstance(bar, ResampledBar):
            raise TypeError("bars must contain ResampledBar values")
        if bar.status is BarStatus.CLOSED and normalized_now < bar.expected_close:
            raise ValueError("cannot backdate CLOSED bar evidence before its expected close")
        normalized_bars.append(bar)

    for bar in normalized_bars:
        if bar.opens_at != normalized_now:
            continue
        same_session_exact_close = any(
            other.session == bar.session and other.expected_close == normalized_now
            for other in normalized_bars
        )
        if not same_session_exact_close:
            return bar

    selected: ResampledBar | None = None
    for bar in normalized_bars:
        if normalized_now < bar.opens_at:
            continue
        if bar.opens_at <= normalized_now < bar.expected_close:
            return bar
        if normalized_now == bar.expected_close:
            return bar
        if normalized_now > bar.expected_close:
            selected = bar
    return selected
