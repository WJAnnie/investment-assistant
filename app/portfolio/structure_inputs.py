"""Bridge validated chart series into multi-cycle structure inputs.

Pure Python standard library: this module never fetches, never resamples and
never invents a bar. It only (1) re-stamps a dated bar's own timestamp to the
instant that bar closed and (2) labels the bar with the status the caller has
already verified. OHLCV values are copied verbatim.

A cycle whose series could not be verified is simply omitted by the caller, so
the structure engine observes it as missing and returns WAIT (R08). Absence is
expressed by not calling this module, never by fabricating a placeholder bar.

Convention used by the re-stamping step: a provider's daily/weekly line carries
the date the bar belongs to, not an instant. A closed daily bar becomes usable
structure input at its own session close (CN 15:00, HK 16:10), so the stamped
instant is derived from the bar's date and the market's close, and always stays
on that bar's own date.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.domain.bars import BarStatus
from app.domain.timeframe import Timeframe
from app.portfolio.structure import CycleInput

SHANGHAI = ZoneInfo("Asia/Shanghai")
HONG_KONG = ZoneInfo("Asia/Hong_Kong")

# A closed bar is usable once its own session has ended.
MARKET_CLOSES: dict[str, tuple[time, ZoneInfo]] = {
    "CN": (time(15, 0), SHANGHAI),
    "HK": (time(16, 10), HONG_KONG),
}

DATED_TIMEFRAMES: tuple[Timeframe, ...] = (Timeframe.WEEKLY, Timeframe.DAILY)
MINUTE_TIMEFRAMES: tuple[Timeframe, ...] = (
    Timeframe.MIN_120,
    Timeframe.MIN_30,
    Timeframe.MIN_15,
    Timeframe.MIN_5,
)


def _validate_timeframe(timeframe, allowed, field):
    if not isinstance(timeframe, Timeframe):
        raise TypeError(f"{field} must be a Timeframe, got {type(timeframe).__name__}")
    if timeframe not in allowed:
        raise ValueError(f"{field} {timeframe.value} is not supported here")


def _validate_source(source):
    if not isinstance(source, str):
        raise TypeError("source must be a string")
    if not source.strip():
        raise ValueError("source cannot be empty or blank")


def _market_close(market):
    if not isinstance(market, str):
        raise TypeError("market must be a string")
    try:
        return MARKET_CLOSES[market]
    except KeyError:
        raise ValueError(f"market {market!r} has no verified close convention") from None


def _referenced_date(value):
    """Return the calendar date a provider line refers to, or raise."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.date()
        return value.astimezone(SHANGHAI).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("bar time cannot be blank")
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
        try:
            return datetime.strptime(text, "%Y/%m/%d").date()
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"bar time is not a recognizable date: {text!r}") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return parsed.date()
        return parsed.astimezone(SHANGHAI).date()
    raise TypeError(f"bar time must be a date or date string, got {type(value).__name__}")


def _restamp(line, close_time, tz):
    return KLine(
        time=datetime.combine(_referenced_date(line.time), close_time, tzinfo=tz).isoformat(),
        open=line.open,
        high=line.high,
        low=line.low,
        close=line.close,
        volume=getattr(line, "volume", 0),
    )


def cycle_from_dated_lines(timeframe, lines, *, market, source):
    """Build a CLOSED CycleInput from validated daily/weekly lines.

    Callers must have already removed unfinished bars (see
    app.portfolio.analysis._filter_completed_lines); this function does not
    re-derive session completeness and never drops a line on its own.
    """
    _validate_timeframe(timeframe, DATED_TIMEFRAMES, "timeframe")
    _validate_source(source)
    close_time, tz = _market_close(market)
    if not isinstance(lines, (list, tuple)):
        raise TypeError(f"lines must be a list or tuple, got {type(lines).__name__}")
    if not lines:
        raise ValueError("lines cannot be empty; omit the cycle instead")

    stamped = []
    previous_date = None
    for index, line in enumerate(lines):
        if not isinstance(line, KLine):
            raise TypeError(
                f"lines[{index}] must be a KLine, got {type(line).__name__}"
            )
        line_date = _referenced_date(line.time)
        if previous_date is not None and line_date <= previous_date:
            raise ValueError(
                f"{timeframe.value} lines must be strictly increasing by date"
            )
        previous_date = line_date
        stamped.append(_restamp(line, close_time, tz))

    return CycleInput(bars=tuple(stamped), status=BarStatus.CLOSED, source=source)


def cycle_from_minute_bars(timeframe, bars, *, status, source):
    """Build a CycleInput from resampled minute bars the caller has classified.

    The status is the caller's verified verdict for the most recent bucket: a
    bucket still filling must arrive as FORMING, a gap as MISSING, and so on.
    """
    _validate_timeframe(timeframe, MINUTE_TIMEFRAMES, "timeframe")
    _validate_source(source)
    if not isinstance(status, BarStatus):
        raise TypeError(f"status must be a BarStatus, got {type(status).__name__}")
    if not isinstance(bars, (list, tuple)):
        raise TypeError(f"bars must be a list or tuple, got {type(bars).__name__}")
    if not bars:
        raise ValueError("bars cannot be empty; omit the cycle instead")
    for index, bar in enumerate(bars):
        if not isinstance(bar, KLine):
            raise TypeError(f"bars[{index}] must be a KLine, got {type(bar).__name__}")

    return CycleInput(bars=tuple(bars), status=status, source=source)


def assemble_cycles(*cycles):
    """Collect CycleInputs keyed by timeframe, rejecting duplicates.

    Missing cycles stay missing: the structure engine must observe them as
    unavailable rather than receive a synthesized stand-in.
    """
    assembled: dict[Timeframe, CycleInput] = {}
    for cycle in cycles:
        if cycle is None:
            continue
        if not isinstance(cycle, tuple) or len(cycle) != 2:
            raise TypeError("each cycle must be a (Timeframe, CycleInput) pair")
        timeframe, value = cycle
        if not isinstance(timeframe, Timeframe):
            raise TypeError(f"cycle key must be a Timeframe, got {type(timeframe).__name__}")
        if not isinstance(value, CycleInput):
            raise TypeError(f"cycle value must be a CycleInput, got {type(value).__name__}")
        if timeframe in assembled:
            raise ValueError(f"duplicate cycle {timeframe.value}")
        assembled[timeframe] = value
    return assembled


__all__ = [
    "MARKET_CLOSES",
    "DATED_TIMEFRAMES",
    "MINUTE_TIMEFRAMES",
    "assemble_cycles",
    "cycle_from_dated_lines",
    "cycle_from_minute_bars",
]

