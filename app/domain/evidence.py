"""Stage 0 unified analysis evidence contracts (Stage 0, R07).

This module provides immutable, audited domain evidence stamps and gate
evidence containers for industry, fundamental, valuation, and multi-cycle
structure analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class EvidenceStatus(str, Enum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    NOT_READY = "NOT_READY"
    FAILED = "FAILED"


class Freshness(str, Enum):
    RECENT = "RECENT"
    DELAYED = "DELAYED"
    STALE = "STALE"
    FUTURE = "FUTURE"
    UNKNOWN = "UNKNOWN"


MAX_SOURCE_LENGTH: int = 128

REASON_CODES: tuple[str, ...] = (
    "DATA_MISSING",
    "MISSING_DATA",
    "DATA_STALE",
    "STALE_DATA",
    "DATA_FUTURE",
    "FUTURE_DATA",
    "CUTOFF_EXCEEDED",
    "SOURCE_UNAVAILABLE",
    "SOURCE_FAILED",
    "SOURCE_DEGRADED",
    "INCOMPLETE_DATA",
    "GATE_INCOMPLETE",
    "DEGRADED_QUALITY",
    "DEGRADED_COVERAGE",
    "UNCONFIRMED_STRUCTURE",
    "VALIDATION_FAILED",
    "TIMEOUT",
    "NOT_READY",
    "FAILED",
    "UNKNOWN",
    "INDUSTRY_DATA_MISSING",
    "INDUSTRY_UNAVAILABLE",
    "INDUSTRY_INCOMPLETE",
    "FUNDAMENTAL_DATA_MISSING",
    "FUNDAMENTAL_UNAVAILABLE",
    "FUNDAMENTAL_INCOMPLETE",
    "VALUATION_DATA_MISSING",
    "VALUATION_UNAVAILABLE",
    "VALUATION_INCOMPLETE",
    "STRUCTURE_DATA_MISSING",
    "STRUCTURE_UNAVAILABLE",
    "STRUCTURE_INCOMPLETE",
)


@dataclass(frozen=True)
class EvidenceStamp:
    source: str
    as_of: datetime
    fetched_at: datetime
    cutoff: datetime
    market_date: date
    status: EvidenceStatus
    freshness: Freshness
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, str):
            raise TypeError("source must be a string")
        if not self.source.strip():
            raise ValueError("source cannot be empty or blank")
        if len(self.source) > MAX_SOURCE_LENGTH:
            raise ValueError(
                f"source length ({len(self.source)}) exceeds maximum length of {MAX_SOURCE_LENGTH}"
            )

        for dt_name, dt_val in (
            ("as_of", self.as_of),
            ("fetched_at", self.fetched_at),
            ("cutoff", self.cutoff),
        ):
            if not isinstance(dt_val, datetime):
                raise TypeError(f"{dt_name} must be a datetime instance")
            if dt_val.tzinfo is None or dt_val.tzinfo.utcoffset(dt_val) is None:
                raise ValueError(f"{dt_name} must be timezone-aware")

        if not isinstance(self.market_date, date) or isinstance(self.market_date, datetime):
            raise TypeError("market_date must be a date instance, not datetime or other types")

        if not isinstance(self.status, EvidenceStatus):
            raise TypeError(
                f"status must be an EvidenceStatus enum instance, got {type(self.status).__name__}"
            )
        if not isinstance(self.freshness, Freshness):
            raise TypeError(
                f"freshness must be a Freshness enum instance, got {type(self.freshness).__name__}"
            )

        if self.as_of > self.cutoff:
            raise ValueError(
                f"as_of ({self.as_of.isoformat()}) cannot be later than cutoff ({self.cutoff.isoformat()})"
            )
        if self.as_of > self.fetched_at:
            raise ValueError(
                f"as_of ({self.as_of.isoformat()}) cannot be later than fetched_at ({self.fetched_at.isoformat()})"
            )

        if self.status == EvidenceStatus.READY:
            if self.reason_code is not None:
                raise ValueError("READY evidence must not have a reason_code")
            if self.freshness in (Freshness.FUTURE, Freshness.STALE, Freshness.UNKNOWN):
                raise ValueError(f"READY status cannot have freshness {self.freshness.value}")
        else:
            if self.reason_code is None:
                raise ValueError(f"{self.status.value} evidence requires a reason_code")
            if not isinstance(self.reason_code, str) or self.reason_code not in REASON_CODES:
                raise ValueError(
                    f"reason_code must be one of REASON_CODES, got {self.reason_code!r}"
                )


@dataclass(frozen=True)
class GateEvidence:
    name: str
    stamp: EvidenceStamp
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty string")
        if not isinstance(self.stamp, EvidenceStamp):
            raise TypeError("stamp must be an EvidenceStamp instance")
        if type(self.complete) is not bool:
            raise TypeError("complete must be a strict bool (True or False)")

    @property
    def passed(self) -> bool:
        return (
            self.stamp.status == EvidenceStatus.READY
            and self.complete is True
            and self.stamp.freshness in (Freshness.RECENT, Freshness.DELAYED)
            and self.stamp.reason_code is None
        )


@dataclass(frozen=True)
class AnalysisEvidenceBundle:
    industry: GateEvidence
    fundamental: GateEvidence
    valuation: GateEvidence
    structure: GateEvidence

    def __post_init__(self) -> None:
        for field_name, gate in (
            ("industry", self.industry),
            ("fundamental", self.fundamental),
            ("valuation", self.valuation),
            ("structure", self.structure),
        ):
            if not isinstance(gate, GateEvidence):
                raise TypeError(f"{field_name} must be a GateEvidence instance")
            if gate.name != field_name:
                raise ValueError(
                    f"{field_name} evidence must use the matching gate name"
                )

    @property
    def ready(self) -> bool:
        return (
            self.industry.passed
            and self.fundamental.passed
            and self.valuation.passed
            and self.structure.passed
        )

    @property
    def blocked_by(self) -> tuple[str, ...]:
        codes: list[str] = []
        for gate in (self.industry, self.fundamental, self.valuation, self.structure):
            if not gate.passed:
                code = gate.stamp.reason_code or f"{gate.name.upper()}_INCOMPLETE"
                codes.append(code)
        return tuple(codes)


__all__ = [
    "EvidenceStatus",
    "Freshness",
    "MAX_SOURCE_LENGTH",
    "REASON_CODES",
    "EvidenceStamp",
    "GateEvidence",
    "AnalysisEvidenceBundle",
]
