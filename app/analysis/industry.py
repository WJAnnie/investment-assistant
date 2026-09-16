from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Sequence

from app.domain.evidence import (
    EvidenceStamp,
    EvidenceStatus,
    Freshness,
    GateEvidence,
    MAX_SOURCE_LENGTH,
)

_MAX_CODE_LENGTH: int = 64
_MAX_NAME_LENGTH: int = 64

_STOCK_PREFIXES: tuple[str, ...] = (
    "00", "30", "60", "68", "83", "87", "88", "43", "92", "90",
    "51", "15", "56", "58", "16",
)
_SECTOR_KEYWORDS: frozenset[str] = frozenset({
    "synthetic", "sector", "fixture", "config_sector",
})


def _is_single_stock_symbol(code: str) -> bool:
    if len(code) == 6 and code.isdigit():
        if code.startswith(_STOCK_PREFIXES):
            return True
    return False


def _is_config_sector(text: str) -> bool:
    low = text.lower().strip()
    if low in _SECTOR_KEYWORDS:
        return True
    if low.startswith(("sector:", "sector_", "config_sector:")):
        return True
    return False


def _validate_code(code: Any, field_name: str = "code") -> str:
    if type(code) is not str:
        raise TypeError(f"{field_name} must be a string")
    if code.strip() != code:
        raise ValueError(f"{field_name} cannot contain leading or trailing whitespace")
    if not code:
        raise ValueError(f"{field_name} cannot be empty or blank")
    if len(code) > _MAX_CODE_LENGTH:
        raise ValueError(f"{field_name} exceeds maximum length of {_MAX_CODE_LENGTH}")
    if _is_single_stock_symbol(code):
        raise ValueError("single stock input is not permitted")
    if _is_config_sector(code):
        raise ValueError("config sector input is not permitted")
    return code


def _validate_name(name: Any) -> str:
    if type(name) is not str:
        raise TypeError("name must be a string")
    if name.strip() != name:
        raise ValueError("name cannot contain leading or trailing whitespace")
    if not name:
        raise ValueError("name cannot be empty or blank")
    if len(name) > _MAX_NAME_LENGTH:
        raise ValueError(f"name exceeds maximum length of {_MAX_NAME_LENGTH}")
    if _is_config_sector(name):
        raise ValueError("config sector input is not permitted")
    return name


def _validate_aware_dt(dt_val: Any, field_name: str) -> datetime:
    if not isinstance(dt_val, datetime):
        raise TypeError(f"{field_name} must be a datetime instance")
    if dt_val.tzinfo is None or dt_val.tzinfo.utcoffset(dt_val) is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return dt_val


def _validate_source(source: Any) -> str:
    if type(source) is not str:
        raise TypeError("source must be a string")
    if source.strip() != source:
        raise ValueError("source cannot contain leading or trailing whitespace")
    if not source:
        raise ValueError("source cannot be empty or blank")
    if len(source) > MAX_SOURCE_LENGTH:
        raise ValueError(f"source length exceeds maximum length of {MAX_SOURCE_LENGTH}")
    return source


def _validate_finite_number(val: Any, field_name: str) -> float | Decimal | int:
    if type(val) is bool:
        raise TypeError(f"{field_name} cannot be a boolean")
    if not isinstance(val, (int, float, Decimal)):
        raise TypeError(f"{field_name} must be a number (int, float, or Decimal)")
    if isinstance(val, float) and not math.isfinite(val):
        raise ValueError(f"{field_name} must be finite")
    if isinstance(val, Decimal) and not val.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return val


def _validate_strict_int(val: Any, field_name: str) -> int:
    if type(val) is not int or isinstance(val, bool):
        raise TypeError(f"{field_name} must be a strict int")
    return val


def _to_decimal(v: int | float | Decimal) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    raise TypeError(f"expected int, float, or Decimal, got {type(v).__name__}")


@dataclass(frozen=True)
class IndustryObservation:
    code: str
    name: str
    as_of: datetime
    fetched_at: datetime
    source: str
    return_1d_pct: float | Decimal | int
    return_5d_pct: float | Decimal | int
    return_20d_pct: float | Decimal | int
    turnover_rate: float | Decimal | int
    advancers: int
    decliners: int

    def __post_init__(self) -> None:
        _validate_code(self.code, "code")
        _validate_name(self.name)
        _validate_aware_dt(self.as_of, "as_of")
        _validate_aware_dt(self.fetched_at, "fetched_at")
        _validate_source(self.source)

        if self.as_of > self.fetched_at:
            raise ValueError("as_of cannot be later than fetched_at")

        _validate_finite_number(self.return_1d_pct, "return_1d_pct")
        _validate_finite_number(self.return_5d_pct, "return_5d_pct")
        _validate_finite_number(self.return_20d_pct, "return_20d_pct")
        _validate_finite_number(self.turnover_rate, "turnover_rate")

        if _to_decimal(self.turnover_rate) < Decimal("0"):
            raise ValueError("turnover_rate must be non-negative")

        _validate_strict_int(self.advancers, "advancers")
        _validate_strict_int(self.decliners, "decliners")

        if self.advancers < 0:
            raise ValueError("advancers must be non-negative")
        if self.decliners < 0:
            raise ValueError("decliners must be non-negative")
        if self.advancers + self.decliners <= 0:
            raise ValueError("total constituent count must be greater than 0")


@dataclass(frozen=True)
class IndustryRankingPolicy:
    weight_1d: float | Decimal | int
    weight_5d: float | Decimal | int
    weight_20d: float | Decimal | int
    weight_breadth: float | Decimal | int
    weight_activity: float | Decimal | int
    min_coverage: int
    max_age_hours: int
    min_score: float | Decimal | int

    def __post_init__(self) -> None:
        for w_name, w_val in (
            ("weight_1d", self.weight_1d),
            ("weight_5d", self.weight_5d),
            ("weight_20d", self.weight_20d),
            ("weight_breadth", self.weight_breadth),
            ("weight_activity", self.weight_activity),
        ):
            _validate_finite_number(w_val, w_name)
            if _to_decimal(w_val) < Decimal("0"):
                raise ValueError(f"{w_name} must be non-negative")

        total = (
            _to_decimal(self.weight_1d)
            + _to_decimal(self.weight_5d)
            + _to_decimal(self.weight_20d)
            + _to_decimal(self.weight_breadth)
            + _to_decimal(self.weight_activity)
        )
        if total != Decimal("1"):
            raise ValueError("sum of weights must equal 1")

        _validate_strict_int(self.min_coverage, "min_coverage")
        if self.min_coverage <= 0:
            raise ValueError("min_coverage must be greater than 0")

        _validate_strict_int(self.max_age_hours, "max_age_hours")
        if self.max_age_hours <= 0:
            raise ValueError("max_age_hours must be greater than 0")

        _validate_finite_number(self.min_score, "min_score")
        min_score_dec = _to_decimal(self.min_score)
        if min_score_dec < Decimal("0") or min_score_dec > Decimal("100"):
            raise ValueError("min_score must be between 0 and 100")


@dataclass(frozen=True)
class IndustryRankedItem:
    code: str
    name: str
    rank: int
    score: float | Decimal | int
    observation: IndustryObservation

    def __post_init__(self) -> None:
        _validate_code(self.code, "code")
        _validate_name(self.name)
        _validate_strict_int(self.rank, "rank")
        if self.rank <= 0:
            raise ValueError("rank must be greater than 0")
        _validate_finite_number(self.score, "score")
        score_dec = _to_decimal(self.score)
        if score_dec < Decimal("0") or score_dec > Decimal("100"):
            raise ValueError("score must be between 0 and 100")
        if not isinstance(self.observation, IndustryObservation):
            raise TypeError("observation must be an IndustryObservation instance")
        if self.observation.code != self.code:
            raise ValueError("observation code must match item code")
        if self.observation.name != self.name:
            raise ValueError("observation name must match item name")


def _compute_average_percentiles(values: Sequence[Decimal]) -> list[Decimal]:
    n = len(values)
    if n == 0:
        return []
    result: list[Decimal] = []
    for val in values:
        less_count = sum(1 for x in values if x < val)
        equal_count = sum(1 for x in values if x == val)
        pct = (Decimal(2 * less_count + equal_count + 1) * Decimal(50)) / Decimal(n)
        result.append(pct)
    return result


@dataclass(frozen=True)
class IndustryRankingResult:
    items: tuple[IndustryRankedItem, ...]
    stamp: EvidenceStamp
    coverage: int
    required: int
    policy: IndustryRankingPolicy

    def __post_init__(self) -> None:
        if type(self.items) is not tuple:
            raise TypeError("items must be a tuple")
        for it in self.items:
            if not isinstance(it, IndustryRankedItem):
                raise TypeError("each item in items must be an IndustryRankedItem instance")

        if not isinstance(self.stamp, EvidenceStamp):
            raise TypeError("stamp must be an EvidenceStamp instance")

        _validate_source(self.stamp.source)

        _validate_strict_int(self.coverage, "coverage")
        if self.coverage < 0:
            raise ValueError("coverage must be non-negative")

        _validate_strict_int(self.required, "required")
        if self.required <= 0:
            raise ValueError("required must be greater than 0")

        if not isinstance(self.policy, IndustryRankingPolicy):
            raise TypeError("policy must be an IndustryRankingPolicy instance")

        if self.coverage != len(self.items):
            raise ValueError(f"coverage ({self.coverage}) must equal len(items) ({len(self.items)})")

        if self.required != self.policy.min_coverage:
            raise ValueError(
                f"required ({self.required}) must equal policy.min_coverage ({self.policy.min_coverage})"
            )

        n = len(self.items)
        if n == 0:
            if self.coverage != 0:
                raise ValueError("empty items requires coverage=0")
            if self.stamp.status == EvidenceStatus.READY:
                raise ValueError("empty items cannot have READY status")
        else:
            seen_codes: set[str] = set()
            for it in self.items:
                if it.code in seen_codes:
                    raise ValueError(f"duplicate code found in items: {it.code}")
                seen_codes.add(it.code)

            actual_ranks = tuple(it.rank for it in self.items)
            expected_ranks = tuple(range(1, n + 1))
            if actual_ranks != expected_ranks:
                raise ValueError(f"ranks must be exactly 1..{n}, got {actual_ranks}")

            for i in range(n - 1):
                prev_it = self.items[i]
                next_it = self.items[i + 1]
                prev_score = _to_decimal(prev_it.score)
                next_score = _to_decimal(next_it.score)
                if prev_score < next_score:
                    raise ValueError("items must be sorted by score descending")
                if prev_score == next_score and prev_it.code >= next_it.code:
                    raise ValueError("tied items must be sorted by code ascending")

            cross_source = self.items[0].observation.source
            cross_as_of = self.items[0].observation.as_of
            cross_fetched_at = max(it.observation.fetched_at for it in self.items)

            for it in self.items:
                if it.observation.source != self.stamp.source:
                    raise ValueError("observation source must match stamp source")
                if it.observation.as_of != self.stamp.as_of:
                    raise ValueError("observation as_of must match stamp as_of")

            if self.stamp.source != cross_source:
                raise ValueError("stamp source must match cross-section source")
            if self.stamp.as_of != cross_as_of:
                raise ValueError("stamp as_of must match cross-section as_of")
            if self.stamp.fetched_at != cross_fetched_at:
                raise ValueError("stamp fetched_at must match cross-section max fetched_at")

            if self.stamp.status == EvidenceStatus.READY:
                if self.coverage < self.required:
                    raise ValueError("READY status requires coverage >= required")
                age = self.stamp.cutoff - self.stamp.as_of
                if age > timedelta(hours=self.policy.max_age_hours):
                    raise ValueError("READY status cannot be stale")

    def gate_for(self, industry_code: str) -> GateEvidence:
        _validate_code(industry_code, "industry_code")

        item = next((it for it in self.items if it.code == industry_code), None)
        if item is None:
            stamp = EvidenceStamp(
                source=self.stamp.source if self.items else "industry",
                as_of=self.stamp.as_of,
                fetched_at=self.stamp.fetched_at,
                cutoff=self.stamp.cutoff,
                market_date=self.stamp.market_date,
                status=EvidenceStatus.NOT_READY,
                freshness=Freshness.UNKNOWN,
                reason_code="INDUSTRY_DATA_MISSING",
            )
            return GateEvidence(
                name="industry",
                stamp=stamp,
                complete=False,
                criterion_passed=False,
                criterion_code="INDUSTRY_CRITERION_FAILED",
            )

        if self.stamp.status != EvidenceStatus.READY:
            return GateEvidence(
                name="industry",
                stamp=self.stamp,
                complete=False,
                criterion_passed=False,
                criterion_code="INDUSTRY_CRITERION_FAILED",
            )

        criterion_passed = _to_decimal(item.score) >= _to_decimal(self.policy.min_score)
        criterion_code = None if criterion_passed else "INDUSTRY_CRITERION_FAILED"

        return GateEvidence(
            name="industry",
            stamp=self.stamp,
            complete=True,
            criterion_passed=criterion_passed,
            criterion_code=criterion_code,
        )


def gate_for(result: IndustryRankingResult, industry_code: str) -> GateEvidence:
    if not isinstance(result, IndustryRankingResult):
        raise TypeError("result must be an IndustryRankingResult instance")
    return result.gate_for(industry_code)


def rank_industries(
    observations: Iterable[IndustryObservation],
    cutoff: datetime,
    policy: IndustryRankingPolicy,
) -> IndustryRankingResult:
    _validate_aware_dt(cutoff, "cutoff")
    if not isinstance(policy, IndustryRankingPolicy):
        raise TypeError("policy must be an IndustryRankingPolicy instance")

    obs_list: list[IndustryObservation] = []
    for obs in observations:
        if not isinstance(obs, IndustryObservation):
            raise TypeError("observation must be an IndustryObservation instance")
        obs_list.append(obs)

    seen_codes: set[str] = set()
    for obs in obs_list:
        if obs.code in seen_codes:
            raise ValueError("duplicate industry code found")
        seen_codes.add(obs.code)

    if obs_list:
        source = obs_list[0].source
        if any(obs.source != source for obs in obs_list):
            raise ValueError("all observations must share the same source")

        sample_as_of = obs_list[0].as_of
        if any(obs.as_of != sample_as_of for obs in obs_list):
            raise ValueError("all observations must share the same as_of cross-section")

        if sample_as_of > cutoff:
            raise ValueError("as_of cannot be later than cutoff")

    coverage = len(obs_list)
    required = policy.min_coverage

    if coverage == 0:
        stamp = EvidenceStamp(
            source="industry",
            as_of=cutoff,
            fetched_at=cutoff,
            cutoff=cutoff,
            market_date=cutoff.date(),
            status=EvidenceStatus.DEGRADED,
            freshness=Freshness.UNKNOWN,
            reason_code="DEGRADED_COVERAGE",
        )
        return IndustryRankingResult(
            items=(),
            stamp=stamp,
            coverage=0,
            required=required,
            policy=policy,
        )

    # Compute percentiles for each factor
    vals_1d = [_to_decimal(obs.return_1d_pct) for obs in obs_list]
    vals_5d = [_to_decimal(obs.return_5d_pct) for obs in obs_list]
    vals_20d = [_to_decimal(obs.return_20d_pct) for obs in obs_list]
    vals_breadth = [
        Decimal(obs.advancers) / Decimal(obs.advancers + obs.decliners)
        for obs in obs_list
    ]
    vals_turnover = [_to_decimal(obs.turnover_rate) for obs in obs_list]

    pcts_1d = _compute_average_percentiles(vals_1d)
    pcts_5d = _compute_average_percentiles(vals_5d)
    pcts_20d = _compute_average_percentiles(vals_20d)
    pcts_breadth = _compute_average_percentiles(vals_breadth)
    pcts_activity = _compute_average_percentiles(vals_turnover)

    w_1d = _to_decimal(policy.weight_1d)
    w_5d = _to_decimal(policy.weight_5d)
    w_20d = _to_decimal(policy.weight_20d)
    w_breadth = _to_decimal(policy.weight_breadth)
    w_activity = _to_decimal(policy.weight_activity)

    scored_obs: list[tuple[float, IndustryObservation]] = []
    for i, obs in enumerate(obs_list):
        score_dec = (
            w_1d * pcts_1d[i]
            + w_5d * pcts_5d[i]
            + w_20d * pcts_20d[i]
            + w_breadth * pcts_breadth[i]
            + w_activity * pcts_activity[i]
        )
        score_value = float(score_dec)
        scored_obs.append((score_value, obs))

    # Sort descending by score, code ascending as stable secondary key
    ranked_entries = sorted(scored_obs, key=lambda x: (-x[0], x[1].code))

    items = [
        IndustryRankedItem(
            code=obs.code,
            name=obs.name,
            rank=rank,
            score=score_value,
            observation=obs,
        )
        for rank, (score_value, obs) in enumerate(ranked_entries, start=1)
    ]

    sample_as_of = obs_list[0].as_of
    max_fetched_at = max(obs.fetched_at for obs in obs_list)
    source = obs_list[0].source
    age = cutoff - sample_as_of
    is_stale = age > timedelta(hours=policy.max_age_hours)

    if is_stale:
        status = EvidenceStatus.NOT_READY
        freshness = Freshness.STALE
        reason_code = "DATA_STALE"
    elif coverage < required:
        status = EvidenceStatus.DEGRADED
        freshness = Freshness.RECENT
        reason_code = "DEGRADED_COVERAGE"
    else:
        status = EvidenceStatus.READY
        freshness = Freshness.RECENT
        reason_code = None

    stamp = EvidenceStamp(
        source=source,
        as_of=sample_as_of,
        fetched_at=max_fetched_at,
        cutoff=cutoff,
        market_date=sample_as_of.date(),
        status=status,
        freshness=freshness,
        reason_code=reason_code,
    )

    return IndustryRankingResult(
        items=tuple(items),
        stamp=stamp,
        coverage=coverage,
        required=required,
        policy=policy,
    )


__all__ = [
    "IndustryObservation",
    "IndustryRankingPolicy",
    "IndustryRankedItem",
    "IndustryRankingResult",
    "rank_industries",
    "gate_for",
]
