"""Multi-cycle Chan confirmation engine (R03, R05, R06, R07, R08).

Pure Python standard-library implementation (no pandas/numpy/yaml/network).
Cross-cycle validation across 6 timeframes with fail-closed evidence gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.chan.signal import ChanSignal
from app.domain.bars import BarStatus
from app.domain.evidence import (
    MAX_SOURCE_LENGTH,
    EvidenceStamp,
    EvidenceStatus,
    Freshness,
    GateEvidence,
)
from app.domain.timeframe import Timeframe

SHANGHAI = ZoneInfo("Asia/Shanghai")

ROLE_ORDER: tuple[Timeframe, ...] = (
    Timeframe.WEEKLY,
    Timeframe.DAILY,
    Timeframe.MIN_120,
    Timeframe.MIN_30,
    Timeframe.MIN_15,
    Timeframe.MIN_5,
)

REQUIRED_CYCLES: tuple[Timeframe, ...] = (
    Timeframe.WEEKLY,
    Timeframe.DAILY,
    Timeframe.MIN_120,
    Timeframe.MIN_30,
    Timeframe.MIN_15,
)

CORE_CYCLE: Timeframe = Timeframe.MIN_120

BUY_SIGNALS: frozenset[ChanSignal] = frozenset(
    {
        ChanSignal.FIRST_BUY,
        ChanSignal.CLASS_SECOND_BUY,
        ChanSignal.SECOND_BUY,
        ChanSignal.THIRD_BUY,
    }
)


class ConfirmOutcome(str, Enum):
    CONFIRMED = "CONFIRMED"
    PRECONFIRM = "PRECONFIRM"
    WAIT = "WAIT"


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
class CycleEvidence:
    timeframe: Timeframe
    code: str
    market: str
    bars: tuple[KLine, ...]
    status: BarStatus
    cutoff: datetime
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.timeframe, Timeframe):
            raise TypeError(
                f"timeframe must be a Timeframe enum, got {type(self.timeframe).__name__}"
            )
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("code must be a non-empty string")
        if not isinstance(self.market, str) or not self.market.strip():
            raise ValueError("market must be a non-empty string")
        if not isinstance(self.status, BarStatus):
            raise TypeError(
                f"status must be a BarStatus enum, got {type(self.status).__name__}"
            )
        if not isinstance(self.cutoff, datetime):
            raise TypeError(
                f"cutoff must be a datetime instance, got {type(self.cutoff).__name__}"
            )
        if self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None:
            raise ValueError("cutoff must be timezone-aware")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string")
        if len(self.source) > MAX_SOURCE_LENGTH:
            raise ValueError(
                f"source length exceeds maximum length of {MAX_SOURCE_LENGTH}"
            )
        if not isinstance(self.bars, (tuple, list)):
            raise TypeError(
                f"bars must be a tuple or list of KLine, got {type(self.bars).__name__}"
            )

        normalized_bars: list[KLine] = []
        prev_dt: datetime | None = None
        for b in self.bars:
            if not isinstance(b, KLine):
                raise TypeError(
                    f"bars item must be a KLine instance, got {type(b).__name__}"
                )
            dt = _parse_aware_datetime(b.time)
            if dt > self.cutoff:
                raise ValueError(f"bar time {dt} exceeds cutoff {self.cutoff}")
            if prev_dt is not None and dt <= prev_dt:
                raise ValueError(
                    "bars timestamps must be strictly increasing and not duplicate"
                )
            prev_dt = dt
            normalized_bars.append(b)

        object.__setattr__(self, "bars", tuple(normalized_bars))


@dataclass(frozen=True)
class MultiCycleConfirmResult:
    code: str
    market: str
    cutoff: datetime
    as_of: datetime
    outcome: ConfirmOutcome
    core_signal: ChanSignal
    trend_confirm: bool
    per_cycle: tuple[CycleEvidence, ...]
    closed_cycles: tuple[str, ...]
    forming_cycles: tuple[str, ...]
    unavailable_cycles: tuple[str, ...]
    price_optimization_available: bool
    blocked_by: tuple[str, ...]
    reason_code: str | None
    structure_gate: GateEvidence


def confirm_multi_cycle(
    evidences: Iterable[CycleEvidence],
    *,
    cutoff: datetime,
    core_signal: Any = None,
    trend_confirm: bool = False,
    source: str = "multi_cycle_confirm",
    min_bars_per_cycle: int = 3,
) -> MultiCycleConfirmResult:
    # 校验 cutoff
    if not isinstance(cutoff, datetime):
        raise TypeError(
            f"cutoff must be a datetime instance, got {type(cutoff).__name__}"
        )
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")

    # 校验 trend_confirm
    if type(trend_confirm) is not bool:
        raise TypeError("trend_confirm must be a strict bool (True or False)")

    # 校验 min_bars_per_cycle
    if type(min_bars_per_cycle) is not int or min_bars_per_cycle <= 0:
        if not isinstance(min_bars_per_cycle, int) or type(min_bars_per_cycle) is bool:
            raise TypeError("min_bars_per_cycle must be a positive integer")
        raise ValueError("min_bars_per_cycle must be a positive integer")

    # 校验 source
    if not isinstance(source, str):
        raise TypeError("source must be a string")
    if not source.strip():
        raise ValueError("source cannot be empty or blank")
    if len(source) > MAX_SOURCE_LENGTH:
        raise ValueError(
            f"source length exceeds maximum length of {MAX_SOURCE_LENGTH}"
        )

    # 校验 evidences
    if not hasattr(evidences, "__iter__"):
        raise TypeError("evidences must be an iterable")
    evidences_list = list(evidences)
    if not evidences_list:
        raise ValueError("evidences cannot be empty")

    first_code: str | None = None
    first_market: str | None = None
    evidence_by_tf: dict[Timeframe, CycleEvidence] = {}

    for ev in evidences_list:
        if not isinstance(ev, CycleEvidence):
            raise TypeError(
                f"evidence item must be a CycleEvidence instance, got {type(ev).__name__}"
            )
        if ev.timeframe in evidence_by_tf:
            raise ValueError(
                f"Duplicate timeframe {ev.timeframe.value} in evidences"
            )
        evidence_by_tf[ev.timeframe] = ev

        if first_code is None:
            first_code = ev.code
            first_market = ev.market
        else:
            if ev.code != first_code:
                raise ValueError(
                    f"Inconsistent code across evidences: {ev.code} vs {first_code}"
                )
            if ev.market != first_market:
                raise ValueError(
                    f"Inconsistent market across evidences: {ev.market} vs {first_market}"
                )

        if ev.cutoff != cutoff:
            raise ValueError(
                f"evidence cutoff {ev.cutoff} does not match parameter cutoff {cutoff}"
            )

    # core_signal 归一化：None → 视为 WAIT；str 用 ChanSignal(value) 解析，解析失败不得抛异常穿透，视为 WAIT。
    if core_signal is None:
        norm_core_signal = ChanSignal.WAIT
    elif isinstance(core_signal, ChanSignal):
        norm_core_signal = core_signal
    elif isinstance(core_signal, str):
        try:
            norm_core_signal = ChanSignal(core_signal)
        except ValueError:
            norm_core_signal = ChanSignal.WAIT
    else:
        norm_core_signal = ChanSignal.WAIT

    # 周期状态划分（closed / forming / unavailable）
    closed_cycles_list: list[str] = []
    forming_cycles_list: list[str] = []
    unavailable_cycles_list: list[str] = []

    for tf in ROLE_ORDER:
        ev = evidence_by_tf.get(tf)
        if ev is None:
            unavailable_cycles_list.append(tf.value)
        elif len(ev.bars) < min_bars_per_cycle:
            unavailable_cycles_list.append(tf.value)
        elif ev.status in (BarStatus.MISSING, BarStatus.STALE, BarStatus.INVALID):
            unavailable_cycles_list.append(tf.value)
        elif ev.status is BarStatus.CLOSED:
            closed_cycles_list.append(tf.value)
        elif ev.status is BarStatus.FORMING:
            forming_cycles_list.append(tf.value)
        else:
            unavailable_cycles_list.append(tf.value)

    closed_cycles = tuple(closed_cycles_list)
    forming_cycles = tuple(forming_cycles_list)
    unavailable_cycles = tuple(unavailable_cycles_list)

    # 5m: price_optimization_available = (5m 在场且 CLOSED)
    price_optimization_available = Timeframe.MIN_5.value in closed_cycles

    # blocked_by: 按 ROLE_ORDER 顺序排列的「非 closed 的必需周期」周期名字符串
    blocked_by_list: list[str] = []
    for tf in REQUIRED_CYCLES:
        if tf.value not in closed_cycles:
            blocked_by_list.append(tf.value)

    core_signal_valid = (norm_core_signal in BUY_SIGNALS) and (trend_confirm is True)
    if not core_signal_valid:
        if "core_signal" not in blocked_by_list:
            blocked_by_list.append("core_signal")

    blocked_by = tuple(blocked_by_list)

    # 判定 outcome 与 reason_code
    req_unavailable = [
        tf for tf in REQUIRED_CYCLES if tf.value in unavailable_cycles
    ]
    req_forming = [tf for tf in REQUIRED_CYCLES if tf.value in forming_cycles]

    if req_unavailable:
        outcome = ConfirmOutcome.WAIT
        has_completely_missing = any(
            tf not in evidence_by_tf for tf in req_unavailable
        )
        if has_completely_missing:
            reason_code: str | None = "STRUCTURE_DATA_MISSING"
        else:
            reason_code = "STRUCTURE_INCOMPLETE"
        stamp_status = EvidenceStatus.NOT_READY
        stamp_freshness = Freshness.UNKNOWN
        gate_complete = False
        gate_passed = False
        gate_criterion_code: str | None = "STRUCTURE_CRITERION_FAILED"
    elif req_forming:
        if core_signal_valid:
            outcome = ConfirmOutcome.PRECONFIRM
        else:
            outcome = ConfirmOutcome.WAIT
        reason_code = "UNCONFIRMED_STRUCTURE"
        stamp_status = EvidenceStatus.DEGRADED
        stamp_freshness = Freshness.RECENT
        gate_complete = False
        gate_passed = False
        gate_criterion_code = "STRUCTURE_CRITERION_FAILED"
    else:
        # REQUIRED_CYCLES 全部 closed
        if core_signal_valid:
            outcome = ConfirmOutcome.CONFIRMED
            reason_code = None
            stamp_status = EvidenceStatus.READY
            stamp_freshness = Freshness.RECENT
            gate_complete = True
            gate_passed = True
            gate_criterion_code = None
        else:
            outcome = ConfirmOutcome.WAIT
            reason_code = "UNCONFIRMED_STRUCTURE"
            stamp_status = EvidenceStatus.DEGRADED
            stamp_freshness = Freshness.RECENT
            gate_complete = False
            gate_passed = False
            gate_criterion_code = "STRUCTURE_CRITERION_FAILED"

    # 计算 as_of
    all_bar_times: list[datetime] = []
    for ev in evidences_list:
        for b in ev.bars:
            all_bar_times.append(_parse_aware_datetime(b.time))

    if all_bar_times:
        as_of = max(all_bar_times)
    else:
        as_of = cutoff

    if as_of > cutoff:
        raise ValueError("as_of cannot be later than cutoff")

    # 构建 stamp 和 structure_gate
    market_date = cutoff.astimezone(SHANGHAI).date()
    stamp = EvidenceStamp(
        source=source,
        as_of=as_of,
        fetched_at=cutoff,
        cutoff=cutoff,
        market_date=market_date,
        status=stamp_status,
        freshness=stamp_freshness,
        reason_code=reason_code,
    )
    structure_gate = GateEvidence(
        name="structure",
        stamp=stamp,
        complete=gate_complete,
        criterion_passed=gate_passed,
        criterion_code=gate_criterion_code,
    )

    # per_cycle 按 ROLE_ORDER 排序
    order_map = {tf: i for i, tf in enumerate(ROLE_ORDER)}
    per_cycle = tuple(
        sorted(evidences_list, key=lambda ev: order_map[ev.timeframe])
    )

    return MultiCycleConfirmResult(
        code=first_code or "",
        market=first_market or "",
        cutoff=cutoff,
        as_of=as_of,
        outcome=outcome,
        core_signal=norm_core_signal,
        trend_confirm=trend_confirm,
        per_cycle=per_cycle,
        closed_cycles=closed_cycles,
        forming_cycles=forming_cycles,
        unavailable_cycles=unavailable_cycles,
        price_optimization_available=price_optimization_available,
        blocked_by=blocked_by,
        reason_code=reason_code,
        structure_gate=structure_gate,
    )


__all__ = [
    "ROLE_ORDER",
    "REQUIRED_CYCLES",
    "CORE_CYCLE",
    "BUY_SIGNALS",
    "ConfirmOutcome",
    "CycleEvidence",
    "MultiCycleConfirmResult",
    "confirm_multi_cycle",
]
