"""Same-day stage evidence helpers for portfolio workflow integration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


PRIVATE_ACCOUNT_DATA = "private_account_data"
SCHEMA = "stage_evidence.v1"
MARKET_TZ = ZoneInfo("Asia/Shanghai")
REPORT_KINDS = {"global", "morning", "midday", "trading", "closing"}
MAX_INTRADAY_AGE = timedelta(minutes=10)
MAX_REFERENCE_AGE = timedelta(days=7)


def build_stage_evidence(snapshot, analysis, report_kind, cutoff, generated_at, rule_version):
    """Build a primitive, private evidence baseline from an in-memory snapshot."""
    cutoff = _market_datetime(cutoff, "cutoff")
    generated_at = _market_datetime(generated_at, "generated_at")
    rule_version = _rule_version(rule_version)
    if generated_at < cutoff:
        raise ValueError("generated_at must be greater than or equal to cutoff")
    if not isinstance(report_kind, str) or report_kind not in REPORT_KINDS:
        raise ValueError("unsupported report_kind")
    holdings = [
        _holding_evidence(item, cutoff, report_kind)
        for account in snapshot.accounts for item in account.holdings
    ]
    identity = [_holding_identity(item) for item in holdings]
    evidence = {
        "schema": SCHEMA,
        "account_evidence_classification": PRIVATE_ACCOUNT_DATA,
        "trading_date": cutoff.date().isoformat(),
        "report_kind": str(report_kind),
        "cutoff": cutoff.isoformat(),
        "generated_at": generated_at.isoformat(),
        "rule_version": rule_version,
        "snapshot_fingerprint": _fingerprint({
            "accounts": [
                {
                    "account_id": account.account.account_id,
                    "holdings": [
                        _holding_identity(item) for item in holdings
                        if item["account_id"] == account.account.account_id
                    ],
                }
                for account in snapshot.accounts
            ],
        }),
        "holdings_fingerprint": _fingerprint(identity),
        "holdings": holdings,
        "technical_scores": _technical_scores(analysis),
        "valid": bool(holdings) and all(item["input_status"] == "valid" for item in holdings),
        "input_reasons": sorted({
            reason
            for item in holdings
            for reason in item.get("input_reasons", ())
        }),
    }
    # Integrity, not a source signature: a local writer can recompute this hash.
    evidence["evidence_fingerprint"] = _fingerprint(evidence)
    return evidence


def compare_stage_evidence(current, morning):
    """Compare a current stage against a compatible same-day morning baseline."""
    global_reasons = _comparison_reasons(current, morning)
    if morning is None:
        return {
            "status": "uncomparable",
            "reasons": global_reasons,
            "holdings": [],
        }

    current_holdings = _holdings_or_empty(current)
    if global_reasons:
        return {
            "status": "uncomparable",
            "reasons": global_reasons,
            "holdings": [
                _uncomparable_holding(item, global_reasons)
                for item in current_holdings
            ],
        }

    baseline_by_key = {_holding_key(item): item for item in morning.get("holdings", ())}
    holdings = []
    for item in current_holdings:
        baseline = baseline_by_key.get(_holding_key(item))
        holdings.append(_compare_holding(item, baseline, current, morning))
    current_keys = {_holding_key(item) for item in current_holdings}
    for key, baseline in baseline_by_key.items():
        if key not in current_keys:
            holdings.append(_uncomparable_holding(baseline, ["current_holding_missing"]))
    comparable = sum(item["status"] == "comparable" for item in holdings)
    status = "comparable" if comparable == len(holdings) else "partial" if comparable else "uncomparable"
    return {
        "status": status,
        "reasons": [],
        "baseline_cutoff": morning["cutoff"],
        "current_cutoff": current["cutoff"],
        "holdings": holdings,
        "technical_changes": _compare_technical_scores(current, morning, holdings),
    }


def evaluate_forecasts(forecasts, current):
    """Evaluate only stored prospective, explicit directional forecasts."""
    envelope_reasons = validate_stage_envelope(current, "current")
    if envelope_reasons:
        return {"status": "unable_to_evaluate", "reasons": envelope_reasons, "evaluations": []}
    current_cutoff = _safe_parse_datetime(current["cutoff"])
    forecast_list = list(forecasts) if isinstance(forecasts, (list, tuple)) else []
    if not forecast_list:
        return {
            "status": "unable_to_evaluate",
            "reasons": ["forecast_missing"],
            "evaluations": [],
        }

    current_by_key = {_holding_key(item): item for item in current.get("holdings", ())}
    forecast_keys = [
        (_forecast_key(item.get("target")), item.get("start"), item.get("deadline"))
        for item in forecast_list if isinstance(item, Mapping)
    ]
    evaluations = []
    for forecast in forecast_list:
        reasons = []
        if not isinstance(forecast, Mapping):
            evaluations.append({
                "status": "unable_to_evaluate",
                "reasons": ["forecast_not_mapping"],
                "target": None,
            })
            continue
        target = forecast.get("target")
        key = _forecast_key(target)
        forecast_identity = (key, forecast.get("start"), forecast.get("deadline"))
        if forecast_keys.count(forecast_identity) > 1:
            reasons.append("duplicate_forecast_identity")
        if key is None:
            reasons.append("target_missing")
        if not (forecast.get("prospective") is True and forecast.get("explicit") is True):
            reasons.append("not_prospective_explicit")
        direction = str(forecast.get("direction", "")).lower()
        if direction not in ("up", "down", "flat"):
            reasons.append("direction_missing")
        start_price = _safe_decimal(forecast.get("start_price"))
        if start_price is None or start_price <= 0:
            reasons.append("start_price_missing")
        cutoff = _safe_parse_datetime(forecast.get("cutoff"))
        start = _safe_parse_datetime(forecast.get("start"))
        deadline = _safe_parse_datetime(forecast.get("deadline"))
        recorded_at = _safe_parse_datetime(forecast.get("recorded_at"))
        if cutoff is None or start is None or deadline is None or recorded_at is None:
            reasons.append("time_window_missing")
        elif not cutoff <= start < deadline or recorded_at < cutoff:
            reasons.append("time_window_invalid")
        else:
            if recorded_at > start:
                reasons.append("recorded_after_start")
            if cutoff >= current_cutoff:
                reasons.append("forecast_not_before_current_cutoff")
            if current_cutoff < deadline:
                reasons.append("deadline_not_reached")

        observed = current_by_key.get(key) if key is not None else None
        observed_price = _safe_decimal(observed.get("price")) if observed is not None else None
        if observed_price is None:
            reasons.append("observed_outcome_missing")
        elif validate_holding_observation(observed, current_cutoff, current["report_kind"]):
            reasons.append("observed_outcome_invalid")
        if observed is not None and deadline is not None:
            observed_as_of = _safe_parse_datetime(observed.get("valuation_as_of"))
            endpoint = _close_endpoint(observed, deadline)
            if endpoint is None or deadline != endpoint:
                reasons.append("deadline_not_market_close")
            if observed_as_of is None:
                reasons.append("observed_as_of_missing")
            elif observed_as_of > deadline:
                reasons.append("future_observed_outcome")
            elif observed_as_of < deadline:
                reasons.append("observed_endpoint_incompatible")
        if observed is not None:
            if forecast.get("rule_version") != current.get("rule_version"):
                reasons.append("rule_version_changed")
            if isinstance(target, Mapping):
                if target.get("valuation_source") != observed.get("valuation_source"):
                    reasons.append("source_changed")
                if target.get("holding_config_fingerprint") != observed.get("holding_config_fingerprint"):
                    reasons.append("holding_fingerprint_changed")

        if reasons:
            evaluations.append({
                "status": "unable_to_evaluate",
                "reasons": sorted(set(reasons)),
                "target": dict(target) if isinstance(target, Mapping) else None,
            })
            continue

        observed_change = observed_price - start_price
        hit = (
            (direction == "up" and observed_change > 0)
            or (direction == "down" and observed_change < 0)
            or (direction == "flat" and observed_change == 0)
        )
        evaluations.append({
            "status": "hit" if hit else "miss",
            "reasons": [],
            "target": dict(target),
            "direction": direction,
            "start_price": _decimal_text(start_price),
            "observed_price": _decimal_text(observed_price),
            "observed_change": _decimal_text(observed_change),
        })
    evaluated = sum(item["status"] in {"hit", "miss"} for item in evaluations)
    return {
        "status": "evaluated" if evaluated == len(evaluations) else "partial" if evaluated else "unable_to_evaluate",
        "reasons": [],
        "evaluations": evaluations,
    }


def _comparison_reasons(current, morning):
    if morning is None:
        return ["baseline_missing"]
    reasons = validate_stage_envelope(current, "current")
    reasons.extend(validate_stage_envelope(morning, "baseline"))
    if not isinstance(current, Mapping) or not isinstance(morning, Mapping):
        return sorted(set(reasons))
    if morning.get("report_kind") != "morning":
        reasons.append("baseline_report_kind_invalid")
    if current.get("report_kind") not in ("midday", "trading", "closing"):
        reasons.append("current_report_kind_invalid")
    if current.get("trading_date") != morning.get("trading_date"):
        reasons.append("cross_day")
    if current.get("rule_version") != morning.get("rule_version"):
        reasons.append("rule_version_changed")
    if reasons:
        return sorted(set(reasons))
    current_cutoff = _parse_aware_datetime(current.get("cutoff"), "current.cutoff")
    morning_cutoff = _parse_aware_datetime(morning.get("cutoff"), "morning.cutoff")
    current_generated = _parse_aware_datetime(current.get("generated_at"), "current.generated_at")
    morning_generated = _parse_aware_datetime(morning.get("generated_at"), "morning.generated_at")
    if morning_cutoff >= current_cutoff:
        reasons.append("baseline_not_before_cutoff")
    if current_generated < current_cutoff:
        reasons.append("current_generated_before_cutoff")
    if morning_generated < morning_cutoff:
        reasons.append("baseline_generated_before_cutoff")
    if morning_generated > current_cutoff:
        reasons.append("baseline_generated_after_current_cutoff")
    reasons.extend(_duplicate_reasons(current, "current"))
    reasons.extend(_duplicate_reasons(morning, "baseline"))
    return sorted(set(reasons))


def validate_stage_envelope(evidence, prefix="evidence"):
    """Return structural/integrity errors without trusting serialized flags."""
    if not isinstance(evidence, Mapping):
        return [f"{prefix}_not_mapping"]
    reasons = []
    if evidence.get("schema") != SCHEMA:
        reasons.append(f"{prefix}_schema_invalid")
    if evidence.get("account_evidence_classification") != PRIVATE_ACCOUNT_DATA:
        reasons.append(f"{prefix}_classification_invalid")
    if _rule_version_or_none(evidence.get("rule_version")) is None:
        reasons.append(f"{prefix}_rule_version_invalid")
    cutoff = _safe_parse_datetime(evidence.get("cutoff"))
    generated = _safe_parse_datetime(evidence.get("generated_at"))
    if cutoff is None or generated is None:
        reasons.append(f"{prefix}_time_invalid")
    else:
        if generated < cutoff:
            reasons.append(f"{prefix}_generated_before_cutoff")
        if cutoff.astimezone(MARKET_TZ).date().isoformat() != evidence.get("trading_date"):
            reasons.append(f"{prefix}_trading_date_invalid")
    kind = evidence.get("report_kind")
    if not isinstance(kind, str) or kind not in REPORT_KINDS:
        reasons.append(f"{prefix}_report_kind_invalid")
    holdings = evidence.get("holdings")
    if not isinstance(holdings, (list, tuple)) or not holdings or any(
        not isinstance(item, Mapping) for item in holdings
    ):
        reasons.append(f"{prefix}_holdings_invalid")
    else:
        required = ("account_id", "code", "market", "instrument_type", "valuation_mode", "holding_config_fingerprint")
        if any(not _rule_version_or_none(item.get(key)) for item in holdings for key in required):
            reasons.append(f"{prefix}_holding_identity_invalid")
        try:
            expected = _fingerprint([_holding_identity(item) for item in holdings])
            if evidence.get("holdings_fingerprint") != expected:
                reasons.append(f"{prefix}_holdings_fingerprint_invalid")
        except (ValueError, TypeError, OverflowError, RecursionError):
            reasons.append(f"{prefix}_holdings_fingerprint_invalid")
        reasons.extend(_duplicate_reasons(evidence, prefix))
    scores = evidence.get("technical_scores")
    if not isinstance(scores, (list, tuple)):
        reasons.append(f"{prefix}_technical_scores_invalid")
    else:
        score_keys = set()
        for item in scores:
            if not isinstance(item, Mapping):
                reasons.append(f"{prefix}_technical_scores_invalid")
                continue
            score = _safe_decimal(item.get("score"))
            account_id = _rule_version_or_none(item.get("account_id"))
            code = _rule_version_or_none(item.get("code"))
            key = (account_id, code)
            if (not account_id or not code or item.get("label") != "technical_score"
                    or score is None or not 0 <= score <= 100 or key in score_keys):
                reasons.append(f"{prefix}_technical_scores_invalid")
            score_keys.add(key)
    try:
        digest = _fingerprint({key: value for key, value in evidence.items() if key != "evidence_fingerprint"})
        if evidence.get("evidence_fingerprint") != digest:
            reasons.append(f"{prefix}_fingerprint_invalid")
    except (ValueError, TypeError, OverflowError, RecursionError):
        reasons.append(f"{prefix}_fingerprint_invalid")
    return reasons


def _holdings_or_empty(evidence):
    holdings = evidence.get("holdings") if isinstance(evidence, Mapping) else None
    if not isinstance(holdings, (list, tuple)):
        return []
    return [item for item in holdings if isinstance(item, Mapping)]


def _duplicate_reasons(evidence, prefix):
    seen = set()
    reasons = []
    for item in evidence.get("holdings", ()):
        key = _holding_key(item)
        if key in seen:
            reasons.append(f"{prefix}_duplicate_identity")
        seen.add(key)
    return reasons


def _compare_holding(item, baseline, current_evidence, morning):
    reasons = []
    if baseline is None:
        reasons.append("baseline_holding_missing")
    else:
        if item.get("holding_config_fingerprint") != baseline.get("holding_config_fingerprint"):
            reasons.append("holding_identity_changed")
        if item.get("value_precision") != baseline.get("value_precision"):
            reasons.append("precision_changed")
        if item.get("valuation_source") != baseline.get("valuation_source"):
            reasons.append("source_changed")
        reasons.extend(_holding_input_reasons(item, "current", current_evidence))
        reasons.extend(_holding_input_reasons(baseline, "baseline", morning))
    if reasons:
        return _uncomparable_holding(item, sorted(set(reasons)))

    current_price = _decimal(item["price"])
    baseline_price = _decimal(baseline["price"])
    quantity = _safe_decimal(item.get("quantity"))
    price_change = current_price - baseline_price
    return {
        "status": "comparable",
        "reasons": [],
        "account_id": item["account_id"],
        "code": item["code"],
        "market": item["market"],
        "valuation_mode": item["valuation_mode"],
        "change_label": "since_morning_reference",
        "baseline_price": _decimal_text(baseline_price),
        "current_price": _decimal_text(current_price),
        "price_change": _decimal_text(price_change),
        "value_change": _decimal_text(price_change * quantity)
        if quantity is not None and item.get("value_precision") == "exact" else None,
    }


def _uncomparable_holding(item, reasons):
    return {
        "status": "uncomparable",
        "reasons": sorted(set(reasons)),
        "account_id": item.get("account_id"),
        "code": item.get("code"),
        "market": item.get("market"),
        "valuation_mode": item.get("valuation_mode"),
        "change_label": "since_morning_reference",
        "baseline_price": None,
        "current_price": item.get("price"),
        "price_change": None,
        "value_change": None,
    }


def _holding_input_reasons(item, prefix, evidence):
    reasons = []
    raw_reasons = validate_holding_observation(
        item, _safe_parse_datetime(evidence["cutoff"]), evidence["report_kind"]
    )
    for reason in raw_reasons:
        if reason == "future_input":
            reasons.append(f"{prefix}_has_future_input")
        elif reason == "nonfinite_price":
            reasons.append(f"{prefix}_has_nonfinite_price")
        elif reason in ("stale_input", "freshness_stale", "freshness_failed", "freshness_lagged"):
            reasons.append(f"{prefix}_has_stale_input")
        else:
            reasons.append(f"{prefix}_has_invalid_input")
    return reasons


def _holding_evidence(item, cutoff, report_kind):
    valuation = item.valuation
    as_of = valuation.as_of if valuation is not None else None
    source = valuation.source if valuation is not None else None
    valuation_price = valuation.price if valuation is not None else None
    freshness = valuation.freshness if valuation is not None else None
    price = _safe_decimal(item.current_price)
    evidence = {
        "account_id": str(item.account_id),
        "code": str(item.holding.code),
        "name": str(item.holding.name),
        "market": str(item.holding.market),
        "instrument_type": str(item.holding.instrument_type),
        "valuation_mode": str(item.holding.valuation_mode),
        "quantity": str(item.holding.quantity) if item.holding.quantity is not None else None,
        "holding_config_fingerprint": _fingerprint(_holding_config_identity(item)),
        "price": _decimal_text(price) if price is not None else None,
        "market_value": _decimal_text(_safe_decimal(item.market_value)),
        "value_precision": str(item.value_precision),
        "valuation_source": str(source) if source is not None else None,
        "valuation_as_of": as_of.isoformat() if isinstance(as_of, datetime) else None,
        "valuation_price": _decimal_text(_safe_decimal(valuation_price)),
        "valuation_freshness": freshness,
    }
    reasons = validate_holding_observation(evidence, cutoff, report_kind)
    evidence.update(input_status="valid" if not reasons else "invalid", input_reasons=reasons)
    return evidence


def validate_holding_observation(item, cutoff, report_kind):
    """Revalidate raw observations; serialized input_status is not evidence."""
    reasons = []
    price = _safe_decimal(item.get("price"))
    quote_price = _safe_decimal(item.get("valuation_price"))
    if price is None or quote_price is None:
        reasons.append("nonfinite_price")
    elif price <= 0 or quote_price <= 0:
        reasons.append("nonpositive_price")
    elif price != quote_price:
        reasons.append("valuation_price_mismatch")
    quantity = _safe_decimal(item.get("quantity"))
    if item.get("quantity") is not None and quantity is None:
        reasons.append("nonfinite_quantity")
    elif quantity is not None and quantity <= 0:
        reasons.append("nonpositive_quantity")
    if not _rule_version_or_none(item.get("valuation_source")):
        reasons.append("source_missing")
    freshness = item.get("valuation_freshness")
    if freshness != "fresh":
        reasons.append(f"freshness_{freshness or 'missing'}")
    as_of = _safe_parse_datetime(item.get("valuation_as_of"))
    if as_of is None:
        reasons.append("as_of_missing")
    elif cutoff is None or as_of > cutoff:
        reasons.append("future_input")
    elif report_kind in {"global", "morning"}:
        # A stored observation, NOT proof of previous close or morning P&L.
        if cutoff - as_of > MAX_REFERENCE_AGE:
            reasons.append("stale_input")
    elif item.get("valuation_mode") != "exchange":
        reasons.append("nav_not_intraday")
    else:
        endpoint = _close_endpoint(item, cutoff)
        if endpoint is None:
            reasons.append("listing_venue_unverified")
        elif report_kind == "closing" and cutoff >= endpoint:
            if as_of != endpoint:
                reasons.append("closing_endpoint_missing")
        elif cutoff - as_of > MAX_INTRADAY_AGE or as_of.astimezone(MARKET_TZ).date() != cutoff.astimezone(MARKET_TZ).date():
            reasons.append("stale_input")
    return sorted(set(reasons))


def _close_endpoint(item, cutoff):
    if item.get("valuation_mode") != "exchange" or item.get("market") not in ("CN", "HK"):
        return None
    # A six-digit mainland instrument can have Hong Kong theme exposure.
    # Until listing venue is modeled separately, do not assign it an HK close.
    code = item.get("code", "")
    if item["market"] == "HK" and isinstance(code, str) and code.isdigit() and len(code) == 6:
        return None
    close_hour = 15 if item["market"] == "CN" else 16
    return datetime.combine(cutoff.astimezone(MARKET_TZ).date(), time(close_hour), MARKET_TZ)


def _holding_config_identity(item):
    holding = item.holding
    return {
        "account_id": str(item.account_id),
        "code": str(holding.code),
        "market": str(holding.market),
        "instrument_type": str(holding.instrument_type),
        "valuation_mode": str(holding.valuation_mode),
        "quantity": _decimal_text(_safe_decimal(holding.quantity)),
        "cost_price": _decimal_text(_safe_decimal(holding.cost_price)),
        "baseline_value": _decimal_text(_safe_decimal(holding.baseline_value)),
        "baseline_price": _decimal_text(_safe_decimal(holding.baseline_price)),
    }


def _holding_identity(item):
    return {
        key: item.get(key) for key in (
            "account_id", "code", "market", "instrument_type", "valuation_mode",
            "quantity", "holding_config_fingerprint",
        )
    }


def _technical_scores(analysis):
    if not isinstance(analysis, Mapping):
        return []
    scores = []
    items = analysis.get("items")
    for item in items if isinstance(items, (list, tuple)) else ():
        technical = item.get("technical") if isinstance(item, Mapping) else None
        score = technical.get("score") if isinstance(technical, Mapping) else None
        numeric_score = _safe_decimal(score)
        if numeric_score is None or not 0 <= numeric_score <= 100:
            continue
        scores.append({
            "account_id": str(item.get("account_id")),
            "code": str(item.get("code")),
            "label": "technical_score",
            "score": _primitive_score(score),
        })
    return scores


def _compare_technical_scores(current, morning, holdings):
    allowed = {(item["account_id"], item["code"]) for item in holdings
               if item["status"] == "comparable"}

    def score_map(evidence):
        result, duplicate = {}, set()
        for item in evidence.get("technical_scores", ()):
            if not isinstance(item, Mapping):
                continue
            key = (str(item.get("account_id")), str(item.get("code")))
            if key in result:
                duplicate.add(key)
            score = _safe_decimal(item.get("score"))
            if item.get("label") == "technical_score" and score is not None and 0 <= score <= 100:
                result[key] = score
        return {key: value for key, value in result.items() if key not in duplicate}

    before, after = score_map(morning), score_map(current)
    return [{"account_id": key[0], "code": key[1], "before": _decimal_text(before[key]),
             "after": _decimal_text(after[key]), "label": "technical_score"}
            for key in sorted(allowed & before.keys() & after.keys()) if before[key] != after[key]]


def _holding_key(item):
    return (
        str(item.get("account_id")),
        str(item.get("code")),
        str(item.get("market")),
        str(item.get("valuation_mode")),
    )


def _forecast_key(target):
    if not isinstance(target, Mapping):
        return None
    required = ("account_id", "code", "market", "valuation_mode")
    if any(target.get(field) is None for field in required):
        return None
    return tuple(str(target[field]) for field in required)


def _fingerprint(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _aware_datetime(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _market_datetime(value, name):
    try:
        return _aware_datetime(value, name).astimezone(MARKET_TZ)
    except OverflowError:
        raise ValueError(f"{name} is outside the supported datetime range") from None


def _parse_aware_datetime(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be timezone-aware")
    return _aware_datetime(datetime.fromisoformat(value), name)


def _safe_parse_datetime(value):
    try:
        return _market_datetime(_parse_aware_datetime(value, "forecast time"), "forecast time")
    except (TypeError, ValueError, OverflowError):
        return None


def _decimal(value):
    result = _safe_decimal(value)
    if result is None:
        raise ValueError("value must be finite Decimal-compatible")
    return result


def _safe_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or len(result.as_tuple().digits) > 64 or abs(result.as_tuple().exponent) > 30:
        return None
    return result


def _decimal_text(value):
    if value is None:
        return None
    return format(value, "f")


def _primitive_score(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else format(value, "f")
    return value


def _rule_version(value):
    result = _rule_version_or_none(value)
    if result is None:
        raise ValueError("rule_version must be a non-blank string")
    return result


def _rule_version_or_none(value):
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result or None
