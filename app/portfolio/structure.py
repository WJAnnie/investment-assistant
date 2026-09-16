"""Portfolio structure evidence engine.

Pure Python standard-library implementation (no pandas/numpy/yaml/network).
Assembles upstream verified multi-cycle inputs into CycleEvidence,
evaluates structure freshness and depth, and delegates to confirm_multi_cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from app.chan.models import KLine
from app.chan.multi_cycle_confirm import (
    ROLE_ORDER,
    ConfirmOutcome,
    CycleEvidence,
    MultiCycleConfirmResult,
    confirm_multi_cycle,
)
from app.domain.bars import BarStatus
from app.domain.evidence import MAX_SOURCE_LENGTH
from app.domain.timeframe import Timeframe

DEFAULT_MIN_BARS_BY_CYCLE: dict[Timeframe, int] = {
    Timeframe.WEEKLY: 3,
    Timeframe.DAILY: 3,
    Timeframe.MIN_120: 3,
    Timeframe.MIN_30: 4,
    Timeframe.MIN_15: 8,
    Timeframe.MIN_5: 12,
}

DEFAULT_MAX_LAG_BY_CYCLE: dict[Timeframe, timedelta] = {
    Timeframe.WEEKLY: timedelta(days=7),
    Timeframe.DAILY: timedelta(days=4),
    Timeframe.MIN_120: timedelta(days=1),
    Timeframe.MIN_30: timedelta(days=1),
    Timeframe.MIN_15: timedelta(days=1),
    Timeframe.MIN_5: timedelta(days=1),
}


def _parse_aware_datetime(val: Any) -> datetime:
    if isinstance(val, datetime):
        dt = val
    elif isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val)
        except Exception as exc:
            raise ValueError(f"Invalid ISO datetime string: {val!r}") from exc
    else:
        raise ValueError(
            f"KLine time must be a datetime or ISO string, got {type(val).__name__}"
        )
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"KLine time must be timezone-aware: {val!r}")
    return dt


@dataclass(frozen=True)
class CycleInput:
    bars: tuple[KLine, ...]
    status: BarStatus
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.bars, (tuple, list)):
            raise TypeError(
                f"bars must be a tuple or list of KLine, got {type(self.bars).__name__}"
            )
        if not isinstance(self.status, BarStatus):
            raise TypeError(
                f"status must be a BarStatus enum instance, got {type(self.status).__name__}"
            )
        if not isinstance(self.source, str):
            raise TypeError("source must be a string")
        if not self.source.strip():
            raise ValueError("source cannot be empty or blank")
        if len(self.source) > MAX_SOURCE_LENGTH:
            raise ValueError(
                f"source length exceeds maximum length of {MAX_SOURCE_LENGTH}"
            )
        object.__setattr__(self, "bars", tuple(self.bars))


@dataclass(frozen=True)
class StructurePolicy:
    """Multi-cycle structure policy configuration.

    The policy must cover all 6 cycles in ROLE_ORDER (WEEKLY, DAILY, MIN_120,
    MIN_30, MIN_15, MIN_5) for both min_bars_by_cycle and max_lag_by_cycle
    to guarantee fail-closed evaluation. Partial policies are strictly forbidden.
    """

    min_bars_by_cycle: Mapping[Timeframe, int] = field(
        default_factory=lambda: dict(DEFAULT_MIN_BARS_BY_CYCLE)
    )
    max_lag_by_cycle: Mapping[Timeframe, timedelta] = field(
        default_factory=lambda: dict(DEFAULT_MAX_LAG_BY_CYCLE)
    )
    source: str = "portfolio.structure"

    def __post_init__(self) -> None:
        if not isinstance(self.source, str):
            raise TypeError("source must be a string")
        if not self.source.strip():
            raise ValueError("source cannot be empty or blank")
        if len(self.source) > MAX_SOURCE_LENGTH:
            raise ValueError(
                f"source length exceeds maximum length of {MAX_SOURCE_LENGTH}"
            )

        if not isinstance(self.min_bars_by_cycle, Mapping):
            raise TypeError("min_bars_by_cycle must be a Mapping")
        if not isinstance(self.max_lag_by_cycle, Mapping):
            raise TypeError("max_lag_by_cycle must be a Mapping")

        min_keys = set(self.min_bars_by_cycle.keys())
        lag_keys = set(self.max_lag_by_cycle.keys())

        if min_keys != lag_keys:
            raise ValueError(
                "min_bars_by_cycle and max_lag_by_cycle must have identical key sets"
            )

        role_set = set(ROLE_ORDER)
        if min_keys != role_set:
            missing = role_set - min_keys
            extra = min_keys - role_set
            details: list[str] = []
            if missing:
                missing_names = sorted(tf.value for tf in missing)
                details.append(f"missing: {missing_names}")
            if extra:
                extra_names = sorted(
                    k.value if isinstance(k, Timeframe) else str(k) for k in extra
                )
                details.append(f"extra: {extra_names}")
            raise ValueError(
                f"policy keys must exactly match ROLE_ORDER; {', '.join(details)}"
            )

        for k, v in self.min_bars_by_cycle.items():
            if not isinstance(k, Timeframe):
                raise ValueError(f"key {k!r} is not a Timeframe")
            if type(v) is not int or isinstance(v, bool):
                raise TypeError(
                    f"min_bars_by_cycle value for {k} must be a strict int, got {type(v).__name__}"
                )
            if v <= 0:
                raise ValueError(
                    f"min_bars_by_cycle value for {k} must be positive, got {v}"
                )

        for k, v in self.max_lag_by_cycle.items():
            if not isinstance(k, Timeframe):
                raise ValueError(f"key {k!r} is not a Timeframe")
            if not isinstance(v, timedelta):
                raise TypeError(
                    f"max_lag_by_cycle value for {k} must be a timedelta, got {type(v).__name__}"
                )
            if v <= timedelta(0):
                raise ValueError(
                    f"max_lag_by_cycle value for {k} must be a positive timedelta, got {v}"
                )

        object.__setattr__(
            self,
            "min_bars_by_cycle",
            MappingProxyType(dict(self.min_bars_by_cycle)),
        )
        object.__setattr__(
            self,
            "max_lag_by_cycle",
            MappingProxyType(dict(self.max_lag_by_cycle)),
        )


@dataclass(frozen=True)
class StructureResult:
    outcome: ConfirmOutcome
    ready: bool
    limit: str
    reason_code: str | None
    blocked_by: tuple[str, ...]
    per_cycle_status: Mapping[str, str]
    stale_cycles: tuple[str, ...]
    insufficient_cycles: tuple[str, ...]
    confirm: MultiCycleConfirmResult

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ConfirmOutcome):
            raise TypeError(
                f"outcome must be a ConfirmOutcome enum instance, got {type(self.outcome).__name__}"
            )
        if type(self.ready) is not bool:
            raise TypeError(
                f"ready must be a strict bool, got {type(self.ready).__name__}"
            )
        if not isinstance(self.limit, str):
            raise TypeError(
                f"limit must be a string, got {type(self.limit).__name__}"
            )
        if self.reason_code is not None and not isinstance(self.reason_code, str):
            raise TypeError("reason_code must be None or string")
        if not isinstance(self.blocked_by, (tuple, list)):
            raise TypeError("blocked_by must be a tuple or list of strings")
        object.__setattr__(self, "blocked_by", tuple(self.blocked_by))

        if not isinstance(self.per_cycle_status, Mapping):
            raise TypeError("per_cycle_status must be a Mapping")
        expected_keys = {tf.value for tf in ROLE_ORDER}
        if set(self.per_cycle_status.keys()) != expected_keys:
            raise ValueError(
                f"per_cycle_status keys must exactly match {expected_keys}"
            )
        valid_status_values = {st.value for st in BarStatus}
        for k, v in self.per_cycle_status.items():
            if v not in valid_status_values:
                raise ValueError(
                    f"per_cycle_status value {v!r} for {k} is not a valid BarStatus value"
                )
        object.__setattr__(
            self,
            "per_cycle_status",
            MappingProxyType(dict(self.per_cycle_status)),
        )

        if not isinstance(self.stale_cycles, (tuple, list)):
            raise TypeError("stale_cycles must be a tuple or list of strings")
        object.__setattr__(self, "stale_cycles", tuple(self.stale_cycles))

        if not isinstance(self.insufficient_cycles, (tuple, list)):
            raise TypeError(
                "insufficient_cycles must be a tuple or list of strings"
            )
        object.__setattr__(
            self, "insufficient_cycles", tuple(self.insufficient_cycles)
        )

        if not isinstance(self.confirm, MultiCycleConfirmResult):
            raise TypeError(
                "confirm must be a MultiCycleConfirmResult instance"
            )

        # Invariants check
        if self.ready != (self.confirm.outcome is ConfirmOutcome.CONFIRMED):
            raise ValueError("ready must match confirm.outcome is CONFIRMED")
        if self.outcome != self.confirm.outcome:
            raise ValueError("outcome must match confirm.outcome")
        expected_limit = "confirmed" if self.ready else "unavailable"
        if self.limit != expected_limit:
            raise ValueError(
                f"limit must be {expected_limit!r} when ready={self.ready}"
            )
        if self.reason_code != self.confirm.reason_code:
            raise ValueError("reason_code must match confirm.reason_code")
        if self.blocked_by != self.confirm.blocked_by:
            raise ValueError("blocked_by must match confirm.blocked_by")


def build_structure_evidence(
    code: str,
    market: str,
    cycles: Mapping[Timeframe, CycleInput],
    *,
    cutoff: datetime,
    core_signal: Any = None,
    trend_confirm: bool = False,
    policy: StructurePolicy | None = None,
) -> StructureResult:
    if not isinstance(code, str):
        raise TypeError(f"code must be a string, got {type(code).__name__}")
    if not code.strip():
        raise ValueError("code must be a non-empty string")

    if not isinstance(market, str):
        raise TypeError(f"market must be a string, got {type(market).__name__}")
    if not market.strip():
        raise ValueError("market must be a non-empty string")

    if not isinstance(cutoff, datetime):
        raise TypeError(
            f"cutoff must be a datetime instance, got {type(cutoff).__name__}"
        )
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")

    if not isinstance(cycles, Mapping):
        raise TypeError(f"cycles must be a Mapping, got {type(cycles).__name__}")

    for k, v in cycles.items():
        if not isinstance(k, Timeframe) or k not in ROLE_ORDER:
            raise ValueError(
                f"cycles keys must be Timeframe instances in ROLE_ORDER, got {k!r}"
            )
        if not isinstance(v, CycleInput):
            raise TypeError(
                f"cycles values must be CycleInput instances, got {type(v).__name__}"
            )

    if policy is None:
        resolved_policy = StructurePolicy()
    elif not isinstance(policy, StructurePolicy):
        raise TypeError(
            f"policy must be a StructurePolicy instance, got {type(policy).__name__}"
        )
    else:
        resolved_policy = policy

    stale_cycles_list: list[str] = []
    insufficient_cycles_list: list[str] = []
    effective_status_map: dict[Timeframe, BarStatus] = {}
    evidence_list: list[CycleEvidence] = []

    for tf in ROLE_ORDER:
        if tf not in cycles:
            continue

        inp = cycles[tf]
        effective_status = inp.status
        was_downgraded_to_stale = False

        max_lag = resolved_policy.max_lag_by_cycle[tf]
        min_bars = resolved_policy.min_bars_by_cycle[tf]

        if inp.bars:
            latest = max(_parse_aware_datetime(b.time) for b in inp.bars)
            if (cutoff - latest) > max_lag:
                if effective_status in (BarStatus.CLOSED, BarStatus.FORMING):
                    effective_status = BarStatus.STALE
                    was_downgraded_to_stale = True

        if was_downgraded_to_stale:
            stale_cycles_list.append(tf.value)

        if len(inp.bars) < min_bars:
            insufficient_cycles_list.append(tf.value)

        effective_status_map[tf] = effective_status

        ev = CycleEvidence(
            timeframe=tf,
            code=code,
            market=market,
            bars=inp.bars,
            status=effective_status,
            cutoff=cutoff,
            source=inp.source,
        )
        evidence_list.append(ev)

    confirm_res = confirm_multi_cycle(
        evidence_list,
        cutoff=cutoff,
        core_signal=core_signal,
        trend_confirm=trend_confirm,
        source=resolved_policy.source,
        min_bars_by_cycle=resolved_policy.min_bars_by_cycle,
    )

    ready = confirm_res.outcome is ConfirmOutcome.CONFIRMED
    limit = "confirmed" if ready else "unavailable"

    per_cycle_status: dict[str, str] = {}
    for tf in ROLE_ORDER:
        if tf in effective_status_map:
            per_cycle_status[tf.value] = effective_status_map[tf].value
        else:
            per_cycle_status[tf.value] = BarStatus.MISSING.value

    return StructureResult(
        outcome=confirm_res.outcome,
        ready=ready,
        limit=limit,
        reason_code=confirm_res.reason_code,
        blocked_by=confirm_res.blocked_by,
        per_cycle_status=per_cycle_status,
        stale_cycles=tuple(stale_cycles_list),
        insufficient_cycles=tuple(insufficient_cycles_list),
        confirm=confirm_res,
    )


__all__ = [
    "CycleInput",
    "StructurePolicy",
    "StructureResult",
    "build_structure_evidence",
    "DEFAULT_MIN_BARS_BY_CYCLE",
    "DEFAULT_MAX_LAG_BY_CYCLE",
]
