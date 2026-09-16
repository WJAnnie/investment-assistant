from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.domain.evidence import (
    EvidenceStamp,
    EvidenceStatus,
    Freshness,
    GateEvidence,
    MAX_SOURCE_LENGTH,
)


def _validate_symbol(symbol: Any) -> str:
    if type(symbol) is not str:
        raise TypeError("symbol must be a string")
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("symbol must be a 6-digit A-share code")
    return symbol


def _validate_aware_dt(dt_val: Any, field_name: str) -> datetime:
    if not isinstance(dt_val, datetime):
        raise TypeError(f"{field_name} must be a datetime instance")
    if dt_val.tzinfo is None or dt_val.tzinfo.utcoffset(dt_val) is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return dt_val


def _validate_source(source: Any) -> str:
    if type(source) is not str:
        raise TypeError("source must be a string")
    if not source.strip():
        raise ValueError("source cannot be empty or blank")
    if len(source) > MAX_SOURCE_LENGTH:
        raise ValueError(
            f"source length ({len(source)}) exceeds maximum length of {MAX_SOURCE_LENGTH}"
        )
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


def _to_decimal(v: int | float | Decimal) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        return Decimal(str(v))
    return Decimal(v)


@dataclass(frozen=True)
class StockFundamentalObservation:
    symbol: str
    report_period: date
    published_at: datetime
    fetched_at: datetime
    source: str
    revenue_growth_pct: float | Decimal | int
    net_profit_growth_pct: float | Decimal | int
    roe_pct: float | Decimal | int
    eps: float | Decimal | int

    def __post_init__(self) -> None:
        _validate_symbol(self.symbol)
        _validate_source(self.source)

        # report_period validation: only strict date, reject datetime
        if not isinstance(self.report_period, date) or isinstance(self.report_period, datetime):
            raise TypeError("report_period must be a date instance, not datetime")

        _validate_aware_dt(self.published_at, "published_at")
        _validate_aware_dt(self.fetched_at, "fetched_at")

        if self.report_period > self.published_at.date():
            raise ValueError("report_period cannot be later than published_at")
        if self.published_at > self.fetched_at:
            raise ValueError("published_at cannot be later than fetched_at")

        _validate_finite_number(self.revenue_growth_pct, "revenue_growth_pct")
        _validate_finite_number(self.net_profit_growth_pct, "net_profit_growth_pct")
        _validate_finite_number(self.roe_pct, "roe_pct")
        _validate_finite_number(self.eps, "eps")


@dataclass(frozen=True)
class StockValuationObservation:
    symbol: str
    as_of: datetime
    fetched_at: datetime
    source: str
    pe_ttm: float | Decimal | int
    pb: float | Decimal | int
    ps: float | Decimal | int

    def __post_init__(self) -> None:
        _validate_symbol(self.symbol)
        _validate_source(self.source)
        _validate_aware_dt(self.as_of, "as_of")
        _validate_aware_dt(self.fetched_at, "fetched_at")

        if self.as_of > self.fetched_at:
            raise ValueError("as_of cannot be later than fetched_at")

        _validate_finite_number(self.pe_ttm, "pe_ttm")
        _validate_finite_number(self.pb, "pb")
        _validate_finite_number(self.ps, "ps")


@dataclass(frozen=True)
class FundamentalPolicy:
    min_roe: float | Decimal | int
    min_eps: float | Decimal | int
    min_revenue_growth: float | Decimal | int
    min_profit_growth: float | Decimal | int
    max_age_days: int

    def __post_init__(self) -> None:
        _validate_finite_number(self.min_roe, "min_roe")
        _validate_finite_number(self.min_eps, "min_eps")
        _validate_finite_number(self.min_revenue_growth, "min_revenue_growth")
        _validate_finite_number(self.min_profit_growth, "min_profit_growth")

        if type(self.max_age_days) is not int:
            raise TypeError("max_age_days must be an integer, not bool, float, or string")
        if self.max_age_days <= 0:
            raise ValueError("max_age_days must be greater than 0")


@dataclass(frozen=True)
class ValuationPolicy:
    max_pe: float | Decimal | int
    max_pb: float | Decimal | int
    max_ps: float | Decimal | int
    max_age_days: int

    def __post_init__(self) -> None:
        _validate_finite_number(self.max_pe, "max_pe")
        _validate_finite_number(self.max_pb, "max_pb")
        _validate_finite_number(self.max_ps, "max_ps")

        if _to_decimal(self.max_pe) <= Decimal(0):
            raise ValueError("max_pe must be greater than 0")
        if _to_decimal(self.max_pb) <= Decimal(0):
            raise ValueError("max_pb must be greater than 0")
        if _to_decimal(self.max_ps) <= Decimal(0):
            raise ValueError("max_ps must be greater than 0")

        if type(self.max_age_days) is not int:
            raise TypeError("max_age_days must be an integer, not bool, float, or string")
        if self.max_age_days <= 0:
            raise ValueError("max_age_days must be greater than 0")


def evaluate_fundamental(
    observation: StockFundamentalObservation | None,
    cutoff: datetime,
    policy: FundamentalPolicy,
) -> GateEvidence:
    _validate_aware_dt(cutoff, "cutoff")
    if not isinstance(policy, FundamentalPolicy):
        raise TypeError("policy must be a FundamentalPolicy instance")

    if observation is None:
        stamp = EvidenceStamp(
            source="fundamental",
            as_of=cutoff,
            fetched_at=cutoff,
            cutoff=cutoff,
            market_date=cutoff.date(),
            status=EvidenceStatus.NOT_READY,
            freshness=Freshness.UNKNOWN,
            reason_code="FUNDAMENTAL_DATA_MISSING",
        )
        return GateEvidence(
            name="fundamental",
            stamp=stamp,
            complete=False,
            criterion_passed=False,
            criterion_code="FUNDAMENTAL_CRITERION_FAILED",
        )

    if not isinstance(observation, StockFundamentalObservation):
        raise TypeError("observation must be a StockFundamentalObservation instance")

    # Reject future data
    if observation.published_at > cutoff:
        raise ValueError("published_at cannot be later than cutoff")
    if observation.published_at > observation.fetched_at:
        raise ValueError("published_at cannot be later than fetched_at")

    # Freshness & staleness evaluation
    age = cutoff - observation.published_at
    is_stale = age > timedelta(days=policy.max_age_days)

    if is_stale:
        status = EvidenceStatus.NOT_READY
        freshness = Freshness.STALE
        reason_code = "DATA_STALE"
    else:
        status = EvidenceStatus.READY
        freshness = Freshness.RECENT
        reason_code = None

    stamp = EvidenceStamp(
        source=observation.source,
        as_of=observation.published_at,
        fetched_at=observation.fetched_at,
        cutoff=cutoff,
        market_date=observation.report_period,
        status=status,
        freshness=freshness,
        reason_code=reason_code,
    )

    # Criterion evaluation
    roe_ok = _to_decimal(observation.roe_pct) >= _to_decimal(policy.min_roe)
    eps_ok = _to_decimal(observation.eps) >= _to_decimal(policy.min_eps)
    rev_ok = _to_decimal(observation.revenue_growth_pct) >= _to_decimal(policy.min_revenue_growth)
    prof_ok = _to_decimal(observation.net_profit_growth_pct) >= _to_decimal(policy.min_profit_growth)

    criterion_passed = bool(roe_ok and eps_ok and rev_ok and prof_ok)
    criterion_code = None if criterion_passed else "FUNDAMENTAL_CRITERION_FAILED"

    return GateEvidence(
        name="fundamental",
        stamp=stamp,
        complete=True,
        criterion_passed=criterion_passed,
        criterion_code=criterion_code,
    )


def evaluate_valuation(
    observation: StockValuationObservation | None,
    cutoff: datetime,
    policy: ValuationPolicy,
) -> GateEvidence:
    _validate_aware_dt(cutoff, "cutoff")
    if not isinstance(policy, ValuationPolicy):
        raise TypeError("policy must be a ValuationPolicy instance")

    if observation is None:
        stamp = EvidenceStamp(
            source="valuation",
            as_of=cutoff,
            fetched_at=cutoff,
            cutoff=cutoff,
            market_date=cutoff.date(),
            status=EvidenceStatus.NOT_READY,
            freshness=Freshness.UNKNOWN,
            reason_code="VALUATION_DATA_MISSING",
        )
        return GateEvidence(
            name="valuation",
            stamp=stamp,
            complete=False,
            criterion_passed=False,
            criterion_code="VALUATION_CRITERION_FAILED",
        )

    if not isinstance(observation, StockValuationObservation):
        raise TypeError("observation must be a StockValuationObservation instance")

    # Reject future data
    if observation.as_of > cutoff:
        raise ValueError("as_of cannot be later than cutoff")
    if observation.as_of > observation.fetched_at:
        raise ValueError("as_of cannot be later than fetched_at")

    # Freshness & staleness evaluation
    age = cutoff - observation.as_of
    is_stale = age > timedelta(days=policy.max_age_days)

    if is_stale:
        status = EvidenceStatus.NOT_READY
        freshness = Freshness.STALE
        reason_code = "DATA_STALE"
    else:
        status = EvidenceStatus.READY
        freshness = Freshness.RECENT
        reason_code = None

    stamp = EvidenceStamp(
        source=observation.source,
        as_of=observation.as_of,
        fetched_at=observation.fetched_at,
        cutoff=cutoff,
        market_date=observation.as_of.date(),
        status=status,
        freshness=freshness,
        reason_code=reason_code,
    )

    # Criterion evaluation
    # 负PE等有效数据只能criterion false，不能冒充缺失
    pe = _to_decimal(observation.pe_ttm)
    pb = _to_decimal(observation.pb)
    ps = _to_decimal(observation.ps)

    pe_ok = (pe > Decimal(0)) and (pe <= _to_decimal(policy.max_pe))
    pb_ok = (pb > Decimal(0)) and (pb <= _to_decimal(policy.max_pb))
    ps_ok = (ps > Decimal(0)) and (ps <= _to_decimal(policy.max_ps))

    criterion_passed = bool(pe_ok and pb_ok and ps_ok)
    criterion_code = None if criterion_passed else "VALUATION_CRITERION_FAILED"

    return GateEvidence(
        name="valuation",
        stamp=stamp,
        complete=True,
        criterion_passed=criterion_passed,
        criterion_code=criterion_code,
    )


__all__ = [
    "StockFundamentalObservation",
    "StockValuationObservation",
    "FundamentalPolicy",
    "ValuationPolicy",
    "evaluate_fundamental",
    "evaluate_valuation",
]
