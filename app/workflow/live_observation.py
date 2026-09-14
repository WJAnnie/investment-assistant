"""Bridge private scheduler observations to the frozen, unverified ledger.

This adapter records failures too. It cannot attest market completeness, user
reviews, ChatGPT consumption or device delivery, and never manufactures IDs.
"""

from datetime import datetime
import hashlib

from app.utils.private_storage import require_private_execution
from app.workflow.live_acceptance import LiveTrialLedger, SlotEvent, TRUSTED_LIVE_MODE
from app.workflow.stage_evidence import MARKET_TZ, validate_holding_observation
from app.workflow.stage_store import STAGE_TIMES, StageStore


def observe_report(ledger, result, *, private_state_dir, scheduled_at, observed_at, clock=None):
    """Append facts after the delivery attempt; preserve its original outcome."""
    result["live_acceptance_status"] = "record_failed"
    try:
        require_private_execution()
        if not isinstance(ledger, LiveTrialLedger):
            raise TypeError("a frozen live ledger is required")
        for value in (scheduled_at, observed_at):
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise ValueError("observation times must be timezone-aware")
        scheduled_at = scheduled_at.astimezone(MARKET_TZ)
        observed_at = observed_at.astimezone(MARKET_TZ)
        kind = result.get("report_kind")
        if (kind not in ("morning", "midday", "trading", "closing")
                or scheduled_at.time() != STAGE_TIMES[kind] or observed_at < scheduled_at):
            raise ValueError("invalid observation slot")
        day = scheduled_at.date().isoformat()
        if day not in ledger.trial_dates:
            raise ValueError("observation is outside the frozen trial")
        evidence = _persisted_evidence(result, private_state_dir, day, kind, observed_at)
        report = result.get("report")
        report_present = isinstance(report, str) and bool(report.strip())
        report_valid = (evidence is not None and report_present
                        and result.get("status") in ("completed", "partial")
                        and result.get("rule_version") == evidence["rule_version"])
        delivery = result.get("delivery")
        api_accepted = (isinstance(delivery, dict) and delivery.get("api_accepted") is True
                        and result.get("notified") is True
                        and result.get("notification_attempted") is True)
        event = SlotEvent(
            run_id=result.get("run_id"), trading_date=day, stage=scheduled_at.strftime("%H:%M"),
            rule_version=result.get("rule_version") or "unavailable",
            ingested_at=observed_at, mode=TRUSTED_LIVE_MODE, storage_visibility="private",
            calendar_source=ledger.calendar_source,
            # Full source provenance is not yet implemented. Even a caller's
            # full_analysis_ready=True must not turn this observation complete.
            data_complete=False, report_valid=report_valid, api_accepted=api_accepted,
            chatgpt_received=False, device_received=False, strategy_success=False,
            data_evidence_id=evidence["evidence_fingerprint"] if evidence is not None else None,
            report_fingerprint=hashlib.sha256(report.encode("utf-8")).hexdigest() if report_present else None,
            api_receipt_id=None, chatgpt_ack_id=None, device_receipt_id=None,
            strategy_evidence_id=None, review_evidence_id=None, position_evidence_id=None,
            wait_reason="Full-source provenance and manual strategy/position review are not verified.",
            latency_seconds=(observed_at - scheduled_at).total_seconds(),
        )
        record = ledger.append(event, clock=clock)
        result["live_acceptance_status"] = "recorded_unverified"
        result["live_observation"] = {"entry_hash": record["entry_hash"],
                                      "recorded_at": record["recorded_at"],
                                      "independently_verified": False}
    except Exception as exc:
        result.setdefault("errors", []).append(f"live observation: {type(exc).__name__}")
        if result.get("status") == "completed":
            result["status"] = "partial"
    return result


def _persisted_evidence(result, directory, day, kind, observed_at):
    context = result.get("stage_context")
    if not isinstance(context, dict) or context.get("persistence") != {"status": "stored"}:
        return None
    try:
        record = StageStore(directory).load_stage(day, kind)
        if record is None or record["evidence"] != context.get("evidence"):
            return None
        evidence = record["evidence"]
        if datetime.fromisoformat(record["recorded_at"]) > observed_at:
            return None
        cutoff = datetime.fromisoformat(evidence["cutoff"])
        if any(validate_holding_observation(item, cutoff, kind) for item in evidence["holdings"]):
            return None
        return evidence
    except (OSError, ValueError, TypeError, KeyError):
        return None
