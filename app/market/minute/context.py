"""Validate injected minute snapshots without treating closure as a signal.

No provider is inferred or contacted by this module. Adapters must declare an
explicit trading day and right-end 5m timestamps; unavailable evidence stays
unavailable. Output is serializable reminder context, never order instructions.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.domain.bars import BarStatus
from app.domain.timeframe import Timeframe
from app.market.minute.resample import _validate_price_line, current_bar, resample_minutes
from app.market.minute.session import TradingDay
from app.utils.redaction import redact_secrets


MARKET_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_TIMEFRAMES = (Timeframe.MIN_120, Timeframe.MIN_30, Timeframe.MIN_15, Timeframe.MIN_5)
MANUAL_REVIEW = "WAIT；数据齐全后人工复核 120m/30m/15m 与风险条件，不自动下单。"


@dataclass(frozen=True)
class MinuteSnapshot:
    """An adapter's claims, all validated at the load boundary before use."""

    code: str
    market: str
    day: TradingDay
    lines: list[KLine] | tuple[KLine, ...]
    source: str
    fetched_at: datetime
    base_minutes: int
    timestamp_semantics: str
    history_120m_lines: tuple[KLine, ...] = ()


def load_minute_context(holding, *, snapshot_loader=None, now):
    """Load one holding in isolation; never echo provider exception messages.

    The optional loader is called as loader(holding, now=aware_shanghai_time).
    It must return MinuteSnapshot, not bare rows with guessed time semantics.
    'ready' means current timing evidence is known (it may still be FORMING).
    'input_ready' only means all current buckets are closed and no earlier
    gaps remain; neither field is permission to confirm a signal or buy.
    """
    local_now = _local_time(now)
    if snapshot_loader is None:
        return _unavailable("分钟数据未接入；先配置来源、交易日历与 5m 收盘时间戳语义。",
                            configured=False)
    if holding.market != "CN" or holding.valuation_mode != "exchange":
        return _unavailable("该市场或估值方式尚无经过验证的分钟闭合契约。")
    try:
        snapshot = snapshot_loader(holding, now=local_now)
    except Exception:
        return _unavailable("分钟数据获取失败；请检查数据来源后重试。")
    try:
        return _snapshot_context(snapshot, holding, local_now)
    except (AttributeError, TypeError, ValueError, ArithmeticError):
        return _unavailable(
            "分钟快照校验失败；请核对标的、日历、时点与 5m 收盘时间戳语义。",
            state=BarStatus.INVALID,
        )


def _snapshot_context(snapshot, holding, now):
    if not isinstance(snapshot, MinuteSnapshot):
        raise ValueError("minute loader must return MinuteSnapshot")
    if (not isinstance(snapshot.code, str) or not snapshot.code.strip()
            or snapshot.code != holding.code or snapshot.market != holding.market):
        raise ValueError("snapshot identity mismatch")
    if (not isinstance(snapshot.day, TradingDay) or snapshot.day.market != snapshot.market
            or snapshot.day.market_date != now.date()):
        raise ValueError("explicit matching trading day is required")
    if not isinstance(snapshot.source, str) or not snapshot.source.strip():
        raise ValueError("minute source is required")
    if type(snapshot.base_minutes) is not int or snapshot.base_minutes != 5:
        raise ValueError("only declared 5m input is supported")
    if snapshot.timestamp_semantics != "close":
        raise ValueError("explicit close timestamp semantics are required")
    fetched_at = _local_time(snapshot.fetched_at)
    if fetched_at > now:
        raise ValueError("snapshot was fetched in the future")

    bars = resample_minutes(snapshot.lines, day=snapshot.day, now=now)
    if snapshot.lines:
        # The resampler already validated type, order, timezone and grid.
        latest = snapshot.lines[-1].time
        latest = latest if isinstance(latest, datetime) else datetime.fromisoformat(latest)
        if _local_time(latest) > fetched_at:
            raise ValueError("source bars cannot be newer than the fetch time")

    if not isinstance(snapshot.history_120m_lines, (list, tuple)):
        raise ValueError("history_120m_lines must be a tuple or list")
    prev_hist_t: datetime | None = None
    for h_line in snapshot.history_120m_lines:
        if not isinstance(h_line, KLine):
            raise TypeError("history_120m_lines must contain only KLine instances")
        h_t = h_line.time
        if isinstance(h_t, str):
            try:
                h_dt = datetime.fromisoformat(h_t)
            except Exception as exc:
                raise ValueError(f"invalid ISO time in history 120m line: {h_t!r}") from exc
        elif isinstance(h_t, datetime):
            h_dt = h_t
        else:
            raise TypeError("KLine time must be an ISO string or datetime")
        if h_dt.tzinfo is None or h_dt.utcoffset() is None:
            raise ValueError("history 120m line time must be timezone-aware")
        h_shanghai = _local_time(h_dt)
        if h_shanghai.date() == snapshot.day.market_date:
            raise ValueError("history 120m line date must not match snapshot market_date")
        if h_shanghai > fetched_at:
            raise ValueError("history 120m line cannot be newer than fetched_at")
        if prev_hist_t is not None and h_shanghai <= prev_hist_t:
            raise ValueError("history 120m lines must be strictly increasing")
        prev_hist_t = h_shanghai
        if h_shanghai.second != 0 or h_shanghai.microsecond != 0:
            raise ValueError("history 120m line time must have 0 seconds and microseconds")
        if (h_shanghai.hour, h_shanghai.minute) not in ((11, 30), (15, 0)):
            raise ValueError(
                f"history 120m line timestamp {h_shanghai.time()} is not a valid 120m close (must be 11:30 or 15:00)"
            )
        _validate_price_line(h_line)

    selected = {cycle: current_bar(bars[cycle], now=now) for cycle in MINUTE_TIMEFRAMES}
    cycles = {
        cycle.value: _cycle_summary(bars[cycle], selected[cycle], now)
        for cycle in MINUTE_TIMEFRAMES
    }
    cycles[Timeframe.MIN_120.value]["closed_lines"] = (
        tuple(snapshot.history_120m_lines) + cycles[Timeframe.MIN_120.value]["closed_lines"]
    )
    states = {cycle: details["status"] for cycle, details in cycles.items()}
    input_ready = all(state == BarStatus.CLOSED.value for state in states.values())
    available = all(state in {BarStatus.CLOSED.value, BarStatus.FORMING.value}
                    for state in states.values())
    reason = _context_reason(snapshot.day, now, states, input_ready)
    return {
        "configured": True,
        "status": "ready" if available else "unavailable",
        "reason": reason,
        "source": redact_secrets(snapshot.source.strip(), environ={}),
        "calendar_source": redact_secrets(snapshot.day.source, environ={}),
        "market_date": snapshot.day.market_date.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "bar_status": states,
        "cycles": cycles,
        "input_ready": input_ready,
        "multi_cycle_confirm": False,
        "next_trigger": _next_trigger(snapshot.day, selected[Timeframe.MIN_120], now, available),
    }


def _cycle_summary(bars, selected, now):
    # Future MISSING buckets are expected. Past/current gaps are not, and must
    # not disappear merely because the most recent bucket happens to be full.
    started = [bar for bar in bars if bar.opens_at <= now]
    missing = sum(bar.status == BarStatus.MISSING for bar in started)
    invalid = sum(bar.status == BarStatus.INVALID for bar in started)
    state = selected.status if selected is not None else BarStatus.MISSING
    if invalid:
        state = BarStatus.INVALID
    elif missing:
        state = BarStatus.MISSING
    closed_lines = tuple(
        bar.line for bar in started
        if bar.status is BarStatus.CLOSED and bar.line is not None
    )
    return {
        "status": state.value,
        "current_status": selected.status.value if selected else BarStatus.MISSING.value,
        "session": redact_secrets(selected.session, environ={}) if selected else None,
        "opens_at": selected.opens_at.isoformat() if selected else None,
        "expected_close": selected.expected_close.isoformat() if selected else None,
        "observed": selected.observed if selected else 0,
        "expected": selected.expected if selected else 0,
        "missing_buckets": missing,
        "invalid_buckets": invalid,
        "closed_lines": closed_lines,
    }


def _context_reason(day, now, states, input_ready):
    if not day.sessions:
        return "交易日历明确休市；不生成当日分钟确认，下一交易日仍需日历证据。"
    if now < day.sessions[0].opens_at:
        return "当前尚未开市；开市前没有当日分钟闭合证据。"
    if BarStatus.INVALID.value in states.values():
        return "分钟周期存在无效数据或不足目标周期的时段；不能用于结构确认。"
    if BarStatus.MISSING.value in states.values():
        return "分钟数据缺失或延迟；即使已到收盘时间，也必须等待数据齐全。"
    if input_ready:
        return "当前分钟输入已闭合，不代表多周期结构确认或买点成立。"
    return "分钟周期仍在形成；等待目标时段结束且数据齐全后再人工复核。"


def _next_trigger(day, anchor, now, available):
    when = None
    condition = "补齐并校验分钟数据；数据齐全后重新检查，不用钟表时间代替数据到达。"
    if not day.sessions:
        condition = "休市；等待经核实的下一交易日与数据齐全，不推算未知交易日。"
    elif now < day.sessions[0].opens_at:
        when = day.sessions[0].opens_at
        condition = "实际开市后观察分钟数据；目标周期结束且数据齐全后再复核。"
    elif anchor is not None and anchor.status != BarStatus.INVALID and anchor.expected_close > now:
        when = anchor.expected_close
        condition = "目标 120m 时段结束且数据齐全；先补齐任何已到期但缺失的基础 K 线。"
    elif available:
        future_sessions = [session for session in day.sessions if session.opens_at > now]
        if future_sessions:
            when = future_sessions[0].opens_at
            condition = "新时段开市后重新观察；目标周期结束且数据齐全后再复核。"
        else:
            condition = "当前分钟数据齐全后仅作人工复盘；下一交易日需另行核实日历。"
    return {"at": when.isoformat() if when else None, "condition": condition, "action": MANUAL_REVIEW}


def _unavailable(reason, *, state=BarStatus.MISSING, configured=True):
    return {
        "configured": configured,
        "status": "unavailable",
        "reason": reason,
        "bar_status": {cycle.value: state.value for cycle in MINUTE_TIMEFRAMES},
        "cycles": {},
        "input_ready": False,
        "multi_cycle_confirm": False,
        "next_trigger": {
            "at": None,
            "condition": "先补齐来源、日历及数据；数据齐全且校验通过后再复核。",
            "action": MANUAL_REVIEW,
        },
    }


def _local_time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(MARKET_TZ)
