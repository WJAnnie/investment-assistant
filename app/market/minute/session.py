"""Explicit CN trading-session calendar primitives for minute resampling.

This module does not infer holidays or trading status. Callers must pass an
auditable source and literal booleans so closed days and half days are
evidence-backed rather than guessed from weekdays.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_MINUTES = 5


def _require_exact_bool(value, name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a literal bool")


def _require_exact_date(value, name: str) -> None:
    if type(value) is not date:
        raise TypeError(f"{name} must be a date, not datetime")


def _normalize_shanghai(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(SHANGHAI)


def _minutes_between(start: datetime, end: datetime) -> int:
    if start.second or start.microsecond or start.minute % BASE_MINUTES != 0:
        raise ValueError("session open must align to a 5-minute boundary")
    if end.second or end.microsecond or end.minute % BASE_MINUTES != 0:
        raise ValueError("session close must align to a 5-minute boundary")
    seconds = (end - start).total_seconds()
    if seconds <= 0 or seconds % 60 != 0:
        raise ValueError("session length must be a positive whole number of minutes")
    return int(seconds // 60)


@dataclass(frozen=True)
class SessionWindow:
    name: str
    opens_at: datetime
    closes_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("session name is required")
        opens_at = _normalize_shanghai(self.opens_at, "opens_at")
        closes_at = _normalize_shanghai(self.closes_at, "closes_at")
        if opens_at.date() != closes_at.date():
            raise ValueError("session must open and close on the same Shanghai date")
        minutes = _minutes_between(opens_at, closes_at)
        if minutes % BASE_MINUTES != 0:
            raise ValueError("session length must align to 5-minute bars")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "opens_at", opens_at)
        object.__setattr__(self, "closes_at", closes_at)


@dataclass(frozen=True)
class TradingDay:
    market: str
    market_date: date
    sessions: tuple[SessionWindow, ...]
    source: str

    def __post_init__(self) -> None:
        if self.market != "CN":
            raise ValueError("only CN market is supported")
        _require_exact_date(self.market_date, "market_date")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("calendar source is required")
        if not isinstance(self.sessions, (list, tuple)):
            raise TypeError("sessions must be a list or tuple")

        sessions = tuple(self.sessions)
        names: set[str] = set()
        previous: SessionWindow | None = None
        for session in sessions:
            if not isinstance(session, SessionWindow):
                raise TypeError("sessions must contain SessionWindow values")
            if session.opens_at.date() != self.market_date or session.closes_at.date() != self.market_date:
                raise ValueError("session date must match trading day")
            if session.name in names:
                raise ValueError("session names must be unique")
            if previous is not None:
                if session.opens_at < previous.closes_at:
                    raise ValueError("sessions must be ordered and nonoverlapping")
                if session.opens_at < previous.opens_at:
                    raise ValueError("sessions must be ordered")
            names.add(session.name)
            previous = session

        object.__setattr__(self, "sessions", sessions)
        object.__setattr__(self, "source", self.source.strip())


def _at(market_date: date, value: time) -> datetime:
    return datetime.combine(market_date, value, tzinfo=SHANGHAI)


def cn_trading_day(market_date, *, is_open, source, half_day=False) -> TradingDay:
    _require_exact_date(market_date, "market_date")
    _require_exact_bool(is_open, "is_open")
    _require_exact_bool(half_day, "half_day")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("calendar source is required")
    if not is_open:
        return TradingDay("CN", market_date, (), source)

    sessions = [
        SessionWindow("morning", _at(market_date, time(9, 30)), _at(market_date, time(11, 30))),
    ]
    if not half_day:
        sessions.append(
            SessionWindow("afternoon", _at(market_date, time(13, 0)), _at(market_date, time(15, 0)))
        )
    return TradingDay("CN", market_date, tuple(sessions), source)
