"""Private five-day observation ledger, not an independent acceptance authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import math
import os
from pathlib import Path
import time as time_module
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from app.utils.private_storage import (
    canonical_json, private_path, read_private_bytes, strict_json, write_private_exclusive,
)


SCHEMA_VERSION = 1
DATA_CLASSIFICATION = "private_live_acceptance"
MARKET_TIMEZONE = "Asia/Shanghai"
STAGES = ("09:00", "11:30", "14:30", "16:10")
TRUSTED_LIVE_MODE = "trusted_private_live"
PRIVATE_VISIBILITY = "private"
MAX_SLOT_DELAY = timedelta(minutes=10)
MAX_FILE_BYTES = 2_000_000
MAX_LINE_BYTES = 100_000
EVIDENCE_FIELDS = (
    "data_evidence_id", "report_fingerprint", "api_receipt_id", "chatgpt_ack_id",
    "device_receipt_id", "strategy_evidence_id", "review_evidence_id", "position_evidence_id",
)
RECEIPT_FIELDS = ("chatgpt_ack_id", "device_receipt_id")


@dataclass(frozen=True)
class SlotEvent:
    """Typed analysis evidence for one trading-day/stage slot."""

    run_id: str
    trading_date: str
    stage: str
    rule_version: str
    ingested_at: datetime
    mode: str
    storage_visibility: str
    calendar_source: str
    data_complete: bool
    report_valid: bool
    api_accepted: bool
    chatgpt_received: bool
    device_received: bool
    strategy_success: bool
    data_evidence_id: str | None
    report_fingerprint: str | None
    api_receipt_id: str | None
    chatgpt_ack_id: str | None
    device_receipt_id: str | None
    strategy_evidence_id: str | None
    review_evidence_id: str | None
    position_evidence_id: str | None
    wait_reason: str = ""
    user_review_second_buy_ok: bool = False
    no_overtrading_ok: bool = False
    position_increment_validated: bool = False
    latency_seconds: float | int | None = None


@dataclass(frozen=True)
class ReceiptEvent:
    """Typed delivery receipt evidence appended after analysis."""

    run_id: str
    trading_date: str
    stage: str
    chatgpt_ack_id: str | None
    device_receipt_id: str | None
    receipt_observed_at: datetime


class LiveTrialLedger:
    """Append-only ledger and status evaluator for the frozen live trial."""

    def __init__(self, path: Path, header: dict):
        self.path = private_path(path)
        self._header_json = _json(_validate_header(header))

    @property
    def header(self):
        return strict_json(self._header_json)

    @property
    def rule_version(self):
        return self.header["rule_version"]

    @property
    def calendar_source(self):
        return self.header["calendar_source"]

    @property
    def ordered_trading_dates(self):
        return tuple(self.header["ordered_trading_dates"])

    @property
    def trial_dates(self):
        return tuple(self.header["trial_dates"])

    @property
    def calendar_verified(self):
        # Compatibility name: a caller's assertion, never an attestation.
        return self.header["calendar_verified"]

    @classmethod
    def create(
        cls,
        path,
        *,
        trial_start: str,
        rule_version: str,
        calendar_source: str,
        ordered_trading_dates: Iterable[str],
        calendar_coverage: Iterable[str],
        calendar_verified: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> "LiveTrialLedger":
        destination = private_path(path)
        dates = _validate_trading_dates(ordered_trading_dates, "ordered_trading_dates")
        coverage = _validate_trading_dates(calendar_coverage, "calendar_coverage")
        if not set(dates).issubset(set(coverage)):
            raise ValueError("calendar coverage must include ordered trading dates")
        start = _parse_date(trial_start, "trial_start").isoformat()
        header = {
            "schema_version": SCHEMA_VERSION,
            "data_classification": DATA_CLASSIFICATION,
            "created_at": _recording_time(clock).isoformat(),
            "trial_start": start,
            "rule_version": _require_text(rule_version, "rule_version"),
            "calendar_source": _require_text(calendar_source, "calendar_source"),
            "calendar_verified": _strict_bool(calendar_verified, "calendar_verified"),
            "calendar_coverage": list(coverage),
            "ordered_trading_dates": list(dates),
            "trial_dates": list(_first_five_dates(start, dates)),
            "market_timezone": MARKET_TIMEZONE,
            "stages": list(STAGES),
            "max_slot_delay_seconds": int(MAX_SLOT_DELAY.total_seconds()),
        }
        _validate_header(header)
        record = {
            "record_type": "header",
            "schema_version": SCHEMA_VERSION,
            "prev_hash": "",
            "header": header,
        }
        record["entry_hash"] = _entry_hash(record)
        try:
            write_private_exclusive(destination, (_json(record) + "\n").encode("utf-8"),
                                    max_bytes=MAX_LINE_BYTES)
        except FileExistsError as exc:
            raise FileExistsError("live acceptance ledger already exists") from exc
        return cls(destination, header)

    @classmethod
    def load(cls, path) -> "LiveTrialLedger":
        destination = private_path(path)
        records = _read_records(destination)
        if not records or records[0]["record_type"] != "header":
            raise ValueError("ledger header missing")
        return cls(destination, records[0]["header"])

    def append(self, event: SlotEvent, *, clock: Callable[[], datetime] | None = None) -> dict:
        _validate_slot_event(event)
        if event.trading_date not in self.ordered_trading_dates:
            raise ValueError("event trading_date is not in frozen trading calendar")
        if event.calendar_source != self.calendar_source:
            raise ValueError("event calendar source mismatch")
        return self._append_record("analysis", _event_payload(event), clock)

    def append_receipt(self, event: ReceiptEvent, *, clock: Callable[[], datetime] | None = None) -> dict:
        _validate_receipt_event(event)
        return self._append_record("receipt", _receipt_payload(event), clock)

    def reserve_attempt(self, run_id: str, trading_date: str, stage: str, *, clock=None) -> dict:
        """Durably claim a slot before work, without claiming a send or result."""
        payload = {"run_id": run_id, "trading_date": trading_date, "stage": stage}
        _validate_attempt_payload(payload)
        return self._append_record("attempt", payload, clock)

    def has_attempt(self, trading_date: str, stage: str) -> bool:
        _parse_date(trading_date, "trading_date")
        _parse_stage_time(stage)
        return any(record["record_type"] == "attempt"
                   and record["event"]["trading_date"] == trading_date
                   and record["event"]["stage"] == stage for record in self._records()[1:])

    def has_analysis(self, trading_date: str, stage: str) -> bool:
        """Check the validated ledger before retrying a slot, not just its successes."""
        _parse_date(trading_date, "trading_date")
        _parse_stage_time(stage)
        return any(record["record_type"] == "analysis"
                   and record["event"]["trading_date"] == trading_date
                   and record["event"]["stage"] == stage for record in self._records()[1:])

    def execution_lock(self):
        """Serialize slot checks through delivery and observation across processes.

        Separate from the append lock so receipts can still be appended. A
        process crash leaves the lock for manual investigation, never a retry.
        """
        return _LedgerLock(self.path.with_suffix(self.path.suffix + ".run"))

    def summary(self, *, as_of: datetime | None = None) -> dict:
        now = _clock_value(as_of)
        records = self._records()
        analyses, receipts, attempts = _group_records(
            record for record in records[1:]
            if _parse_datetime(record["recorded_at"], "recorded_at") <= now
        )
        days = {}
        totals = {"complete": 0, "failed": 0, "pending": 0}
        for day in self.trial_dates:
            slots = {}
            for stage in STAGES:
                status, reasons = _slot_status(
                    analyses.get((day, stage), []),
                    receipts.get((day, stage), []),
                    attempts=attempts.get((day, stage), []),
                    day=day,
                    stage=stage,
                    as_of=now,
                    rule_version=self.rule_version,
                    calendar_source=self.calendar_source,
                    calendar_verified=self.calendar_verified,
                )
                totals[status] += 1
                slots[stage] = {
                    "status": "pending_verification" if status == "complete" else status,
                    "evidence_status": status, "reasons": reasons,
                }
            days[day] = {"slots": slots}
        reasons = [] if self.calendar_verified else ["calendar not independently verified"]
        overall = "complete"
        if totals["failed"] or reasons:
            overall = "failed"
        elif totals["pending"]:
            overall = "pending"
        return {
            "status": "pending_verification" if overall == "complete" else overall,
            "evidence_status": overall,
            "independently_verified": False,
            "trust_boundary": "Local hashes and caller claims are consistency checks, not attestation.",
            "verification_gaps": ["independent calendar source", "data/report provenance",
                                  "ChatGPT and device receipt verification", "strategy and position review"],
            "reasons": reasons,
            "rule_version": self.rule_version,
            "calendar_source": self.calendar_source,
            "calendar_verified": self.calendar_verified,
            "calendar_verification": "caller_claim_only" if self.calendar_verified else "unverified",
            "trial_dates": list(self.trial_dates),
            "stages": list(STAGES),
            "totals": totals,
            "days": days,
        }

    def _append_record(self, record_type: str, payload: dict, clock) -> dict:
        with _LedgerLock(self.path):
            records = self._records()
            # Timestamp in lock order, not thread-arrival order. A real clock
            # rollback is still rejected by the relationship validation.
            recorded_at = _recording_time(clock)
            _validate_relationships(records, record_type, payload, recorded_at)
            record = {
                "record_type": record_type,
                "schema_version": SCHEMA_VERSION,
                "recorded_at": recorded_at.isoformat(),
                "prev_hash": records[-1]["entry_hash"],
                "event": payload,
            }
            record["entry_hash"] = _entry_hash(record)
            line = (_json(record) + "\n").encode("utf-8")
            if len(line) > MAX_LINE_BYTES:
                raise ValueError("ledger record exceeds size cap")
            destination = private_path(self.path)
            if destination.stat().st_size + len(line) > MAX_FILE_BYTES:
                raise ValueError("ledger exceeds size cap")
            fd = os.open(destination, os.O_APPEND | os.O_WRONLY)
            with os.fdopen(fd, "ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            return record

    def _records(self) -> list[dict]:
        records = _read_records(self.path)
        if _json(records[0]["header"]) != self._header_json:
            raise ValueError("frozen ledger header changed")
        return records


def _slot_status(analyses, receipts, *, attempts, day, stage, as_of, rule_version, calendar_source, calendar_verified):
    if not analyses:
        missing = "attempt outcome missing" if attempts else "missing event"
        if as_of > _slot_deadline(day, stage):
            return "failed", [missing if attempts else "missed slot"]
        return "pending", [missing]
    reasons = []
    pending = []
    for event in analyses:
        merged = dict(event)
        for receipt in receipts:
            if receipt["run_id"] == event["run_id"]:
                for field, received in (("chatgpt_ack_id", "chatgpt_received"),
                                        ("device_receipt_id", "device_received")):
                    if receipt.get(field):
                        merged[received] = True
                        merged[field] = receipt[field]
                times = {**merged, "_receipt_recorded_at": receipt["_recorded_at"],
                         "_receipt_observed_at": receipt["receipt_observed_at"]}
                reasons.extend(_time_reasons(times, "_receipt_recorded_at", "receipt record"))
                reasons.extend(_time_reasons(times, "_receipt_observed_at", "receipt observed"))
        current_reasons, current_pending = _event_reasons(
            merged,
            rule_version=rule_version,
            calendar_source=calendar_source,
            calendar_verified=calendar_verified,
        )
        reasons.extend(current_reasons)
        pending.extend(current_pending)
    if reasons:
        return "failed", sorted(set(reasons))
    if pending:
        if as_of > _slot_deadline(day, stage):
            return "failed", sorted(set(pending + ["evidence deadline expired"]))
        return "pending", sorted(set(pending))
    return "complete", []


def _event_reasons(event, *, rule_version, calendar_source, calendar_verified):
    reasons = []
    pending = []
    if not calendar_verified:
        reasons.append("calendar not independently verified")
    if event["rule_version"] != rule_version:
        reasons.append("rule version mismatch")
    if event["calendar_source"] != calendar_source:
        reasons.append("calendar source mismatch")
    if event["mode"] != TRUSTED_LIVE_MODE or event["storage_visibility"] != PRIVATE_VISIBILITY:
        reasons.append("not trusted private live")
    wait = bool(event["wait_reason"])
    for field, reason in (
        ("data_complete", "data completeness missing"),
        ("report_valid", "report validity missing"),
        ("api_accepted", "API acceptance missing"),
    ):
        if event[field] is not True:
            (pending if wait else reasons).append(reason)
    for field, reason in (
        ("chatgpt_received", "ChatGPT receipt pending"),
        ("device_received", "device receipt pending"),
    ):
        if event[field] is not True:
            pending.append(reason)
    if not event["wait_reason"] and event["strategy_success"] is not True:
        reasons.append("strategy success missing")
    if wait and (
        event["data_complete"] is not True
        or event["report_valid"] is not True
        or not event["strategy_evidence_id"]
    ):
        pending.append("WAIT evidence incomplete")
    for field, reason in (
        ("data_evidence_id", "data evidence missing"),
        ("report_fingerprint", "report fingerprint missing"),
        ("api_receipt_id", "API receipt evidence missing"),
        ("strategy_evidence_id", "strategy evidence missing"),
        ("review_evidence_id", "review evidence missing"),
        ("position_evidence_id", "position evidence missing"),
    ):
        if not event.get(field):
            (pending if wait else reasons).append(reason)
    if event["chatgpt_received"] is True and not event.get("chatgpt_ack_id"):
        pending.append("ChatGPT receipt pending")
    if event["device_received"] is True and not event.get("device_receipt_id"):
        pending.append("device receipt pending")
    if event["user_review_second_buy_ok"] is not True:
        reasons.append("second-buy review missing")
    if event["no_overtrading_ok"] is not True:
        reasons.append("overtrading review missing")
    if event["position_increment_validated"] is not True:
        reasons.append("position increment validation missing")
    reasons.extend(_time_reasons(event, "ingested_at", "ingestion"))
    reasons.extend(_time_reasons(event, "_recorded_at", "record"))
    if event.get("_receipt_recorded_at"):
        reasons.extend(_time_reasons(event, "_receipt_recorded_at", "receipt record"))
    if event.get("_receipt_observed_at"):
        reasons.extend(_time_reasons(event, "_receipt_observed_at", "receipt observed"))
    return reasons, pending


def _time_reasons(event, field, label):
    try:
        value = _parse_datetime(event[field], field).astimezone(ZoneInfo(MARKET_TIMEZONE))
    except (KeyError, TypeError, ValueError):
        return [f"invalid {label} time evidence"]
    day = _parse_date(event["trading_date"], "trading_date")
    start = datetime.combine(day, _parse_stage_time(event["stage"]), ZoneInfo(MARKET_TIMEZONE))
    end = start + MAX_SLOT_DELAY
    reasons = []
    if value.date() != day:
        reasons.append(f"{label} wall-clock does not match trading day")
    if not start <= value <= end:
        reasons.append(f"{label} wall-clock outside slot window")
    return reasons


def _group_records(records):
    analyses = {}
    receipts = {}
    attempts = {}
    for record in records:
        event = dict(record["event"])
        event["_recorded_at"] = record["recorded_at"]
        target = {"analysis": analyses, "receipt": receipts, "attempt": attempts}[record["record_type"]]
        target.setdefault((event["trading_date"], event["stage"]), []).append(event)
    return analyses, receipts, attempts


def _validate_relationships(records, record_type, payload, recorded_at):
    header = records[0]["header"]
    if recorded_at < _parse_datetime(header["created_at"], "created_at"):
        raise ValueError("event cannot precede ledger creation")
    if len(records) > 1 and recorded_at < _parse_datetime(records[-1]["recorded_at"], "recorded_at"):
        raise ValueError("ledger clock moved backwards")
    if payload["trading_date"] not in header["ordered_trading_dates"]:
        raise ValueError("event trading_date is not in frozen trading calendar")
    if record_type == "attempt":
        if payload["trading_date"] not in header["trial_dates"]:
            raise ValueError("attempt is outside the frozen trial")
        deadline = _slot_deadline(payload["trading_date"], payload["stage"])
        if not deadline - MAX_SLOT_DELAY <= recorded_at <= deadline:
            raise ValueError("attempt must be reserved inside its slot window")
        for record in records[1:]:
            event = record["event"]
            if (event["run_id"] == payload["run_id"]
                    or all(event[key] == payload[key] for key in ("trading_date", "stage"))):
                raise ValueError("attempt run or slot already recorded")
        return
    observed_field = "ingested_at" if record_type == "analysis" else "receipt_observed_at"
    observed_at = _parse_datetime(payload[observed_field], observed_field)
    if observed_at > recorded_at:
        raise ValueError("observed time cannot be in the future")
    if record_type == "analysis" and payload["calendar_source"] != header["calendar_source"]:
        raise ValueError("event calendar source mismatch")
    parent = None
    for record in records[1:]:
        event = record["event"]
        same_run = event["run_id"] == payload["run_id"]
        if record["record_type"] == "attempt" and record_type == "analysis":
            same_slot = all(event[key] == payload[key] for key in ("trading_date", "stage"))
            if same_run != same_slot:
                raise ValueError("analysis must match its reserved run and slot")
            if same_run and observed_at < _parse_datetime(record["recorded_at"], "recorded_at"):
                raise ValueError("analysis cannot precede its attempt reservation")
        if record["record_type"] == "analysis" and same_run:
            if record_type == "analysis":
                raise ValueError("run_id already recorded")
            parent = record
        for field in RECEIPT_FIELDS:
            if payload.get(field) and event.get(field):
                if same_run or payload[field] == event[field]:
                    raise ValueError("receipt channel or evidence ID already recorded")
    if record_type == "receipt":
        if parent is None or any(parent["event"][key] != payload[key] for key in ("trading_date", "stage")):
            raise ValueError("receipt run_id must reference an existing analysis slot")
        if observed_at < _parse_datetime(parent["recorded_at"], "recorded_at"):
            raise ValueError("receipt cannot precede its analysis record")


def _validate_slot_event(event):
    if not isinstance(event, SlotEvent):
        raise TypeError("event must be a SlotEvent")
    for field in ("run_id", "trading_date", "stage", "rule_version", "mode", "storage_visibility", "calendar_source", "wait_reason"):
        value = getattr(event, field)
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
    _require_text(event.run_id, "run_id")
    for field in ("rule_version", "mode", "storage_visibility", "calendar_source"):
        _require_text(getattr(event, field), field)
    for field in EVIDENCE_FIELDS:
        if getattr(event, field) is not None:
            _require_text(getattr(event, field), field)
    _parse_date(event.trading_date, "trading_date")
    _parse_stage_time(event.stage)
    _validate_datetime(event.ingested_at, "ingested_at")
    for field in (
        "data_complete", "report_valid", "api_accepted", "chatgpt_received",
        "device_received", "strategy_success", "user_review_second_buy_ok",
        "no_overtrading_ok", "position_increment_validated",
    ):
        _strict_bool(getattr(event, field), field)
    if event.latency_seconds is not None:
        if type(event.latency_seconds) not in (int, float):
            raise TypeError("latency_seconds must be numeric")
        if not math.isfinite(event.latency_seconds) or event.latency_seconds < 0:
            raise ValueError("latency_seconds must be finite and nonnegative")


def _validate_receipt_event(event):
    if not isinstance(event, ReceiptEvent):
        raise TypeError("event must be a ReceiptEvent")
    for field in ("run_id", "trading_date", "stage"):
        _require_text(getattr(event, field), field)
    _parse_date(event.trading_date, "trading_date")
    _parse_stage_time(event.stage)
    _validate_datetime(event.receipt_observed_at, "receipt_observed_at")
    for field in RECEIPT_FIELDS:
        if getattr(event, field) is not None:
            _require_text(getattr(event, field), field)
    if not event.chatgpt_ack_id and not event.device_receipt_id:
        raise ValueError("receipt evidence is required")


def _event_payload(event):
    payload = asdict(event)
    payload["ingested_at"] = event.ingested_at.astimezone(ZoneInfo(MARKET_TIMEZONE)).isoformat()
    return payload


def _receipt_payload(event):
    payload = asdict(event)
    payload["receipt_observed_at"] = event.receipt_observed_at.astimezone(ZoneInfo(MARKET_TIMEZONE)).isoformat()
    return payload


def _read_records(path: Path) -> list[dict]:
    data = read_private_bytes(path, max_bytes=MAX_FILE_BYTES)
    if not data.endswith(b"\n"):
        raise ValueError("ledger must end with newline")
    records = []
    prev_hash = ""
    for line_number, line in enumerate(data.splitlines(), 1):
        if len(line) > MAX_LINE_BYTES:
            raise ValueError("ledger line exceeds size cap")
        record = strict_json(line)
        _validate_record_schema(record, line_number)
        expected = _entry_hash({key: value for key, value in record.items() if key != "entry_hash"})
        if record["prev_hash"] != prev_hash or record["entry_hash"] != expected:
            raise ValueError(f"ledger hash chain is invalid at line {line_number}")
        if line_number == 1:
            if record["record_type"] != "header":
                raise ValueError("ledger header missing")
        elif record["record_type"] == "header":
            raise ValueError("ledger header must be the first and only header")
        else:
            _validate_relationships(records, record["record_type"], record["event"],
                                    _parse_datetime(record["recorded_at"], "recorded_at"))
        records.append(record)
        prev_hash = record["entry_hash"]
    return records


def _validate_record_schema(record, line_number):
    if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
            or record.get("schema_version") != SCHEMA_VERSION):
        raise ValueError(f"invalid ledger record schema at line {line_number}")
    if record.get("record_type") == "header":
        if set(record) != {"record_type", "schema_version", "prev_hash", "entry_hash", "header"}:
            raise ValueError("invalid ledger header fields")
        for key in ("prev_hash", "entry_hash", "header"):
            if key not in record:
                raise ValueError("invalid ledger header")
        _validate_header(record["header"])
    elif record.get("record_type") in ("analysis", "receipt", "attempt"):
        if set(record) != {"record_type", "schema_version", "recorded_at", "prev_hash", "entry_hash", "event"}:
            raise ValueError("invalid ledger event fields")
        for key in ("recorded_at", "prev_hash", "entry_hash", "event"):
            if key not in record:
                raise ValueError("invalid ledger event")
        _parse_datetime(record["recorded_at"], "recorded_at")
        if record["record_type"] == "analysis":
            _validate_slot_payload(record["event"])
        elif record["record_type"] == "receipt":
            _validate_receipt_payload(record["event"])
        else:
            _validate_attempt_payload(record["event"])
    else:
        raise ValueError("invalid ledger record type")


def _validate_header(header):
    required = {
        "schema_version": int, "data_classification": str, "created_at": str,
        "trial_start": str, "rule_version": str, "calendar_source": str,
        "calendar_verified": bool, "calendar_coverage": list,
        "ordered_trading_dates": list, "trial_dates": list, "market_timezone": str,
        "stages": list, "max_slot_delay_seconds": int,
    }
    if not isinstance(header, dict):
        raise ValueError("ledger header must be a mapping")
    if set(header) != set(required):
        raise ValueError("ledger header fields are invalid")
    for key, expected in required.items():
        if type(header.get(key)) is not expected:
            raise ValueError(f"ledger header {key} is invalid")
    if header["schema_version"] != SCHEMA_VERSION or header["data_classification"] != DATA_CLASSIFICATION:
        raise ValueError("ledger header classification is invalid")
    _parse_datetime(header["created_at"], "created_at")
    _parse_date(header["trial_start"], "trial_start")
    _require_text(header["rule_version"], "rule_version")
    _require_text(header["calendar_source"], "calendar_source")
    coverage = _validate_trading_dates(header["calendar_coverage"], "calendar_coverage")
    ordered = _validate_trading_dates(header["ordered_trading_dates"], "ordered_trading_dates")
    if tuple(day for day in coverage if ordered[0] <= day <= ordered[-1]) != ordered:
        raise ValueError("ordered calendar must not omit dates from its coverage")
    if _first_five_dates(header["trial_start"], coverage) != tuple(header["trial_dates"]):
        raise ValueError("trial calendar does not match calendar coverage")
    if tuple(header["trial_dates"]) != _first_five_dates(header["trial_start"], tuple(header["ordered_trading_dates"])):
        raise ValueError("ledger header trial dates are invalid")
    if header["market_timezone"] != MARKET_TIMEZONE or tuple(header["stages"]) != STAGES:
        raise ValueError("ledger header stage contract is invalid")
    if header["max_slot_delay_seconds"] != int(MAX_SLOT_DELAY.total_seconds()):
        raise ValueError("ledger header delay contract is invalid")
    start = _slot_deadline(header["trial_dates"][0], STAGES[0]) - MAX_SLOT_DELAY
    if _parse_datetime(header["created_at"], "created_at") > start:
        raise ValueError("ledger must be initialized before trial start")
    return dict(header)


def _validate_slot_payload(payload):
    try:
        data = dict(payload)
        data["ingested_at"] = _parse_datetime(data["ingested_at"], "ingested_at")
        _validate_slot_event(SlotEvent(**data))
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid slot event payload") from exc


def _validate_receipt_payload(payload):
    try:
        data = dict(payload)
        data["receipt_observed_at"] = _parse_datetime(data["receipt_observed_at"], "receipt_observed_at")
        _validate_receipt_event(ReceiptEvent(**data))
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid receipt event payload") from exc


def _validate_attempt_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"run_id", "trading_date", "stage"}:
        raise ValueError("invalid attempt event payload")
    _require_text(payload["run_id"], "run_id")
    _parse_date(payload["trading_date"], "trading_date")
    _parse_stage_time(payload["stage"])


def _validate_trading_dates(values, field):
    dates = tuple(values)
    if len(dates) < 5:
        raise ValueError("calendar must include at least five trading dates")
    parsed = [_parse_date(value, field) for value in dates]
    for day in parsed:
        if day.weekday() >= 5:
            raise ValueError("calendar contains weekend trading date")
    if any(later <= earlier for earlier, later in zip(parsed, parsed[1:])):
        raise ValueError("ordered trading dates must be strictly increasing")
    return tuple(day.isoformat() for day in parsed)


def _first_five_dates(trial_start, trading_dates):
    start = _parse_date(trial_start, "trial_start")
    selected = [day for day in trading_dates if _parse_date(day, "ordered_trading_dates") >= start][:5]
    if len(selected) < 5:
        raise ValueError("calendar does not contain five trading dates from trial start")
    return tuple(selected)


def _slot_deadline(day, stage):
    return datetime.combine(_parse_date(day, "trading_date"), _parse_stage_time(stage), ZoneInfo(MARKET_TIMEZONE)) + MAX_SLOT_DELAY


def _recording_time(clock):
    if clock is None:
        return _clock_value()
    if not callable(clock):
        raise TypeError("clock must be callable")
    try:
        value = clock()
    except Exception:
        raise ValueError("clock sampling failed") from None
    if value is None:
        raise TypeError("clock must return a timezone-aware datetime")
    return _clock_value(value)


def _clock_value(value=None):
    current = datetime.now(ZoneInfo(MARKET_TIMEZONE)) if value is None else value
    if not isinstance(current, datetime):
        raise TypeError("clock must be a timezone-aware datetime")
    try:
        return _validate_datetime(current, "clock").astimezone(ZoneInfo(MARKET_TIMEZONE))
    except Exception:
        raise ValueError("clock must be a valid timezone-aware datetime") from None


def _validate_datetime(value, field):
    if not isinstance(value, datetime) or isinstance(value, bool):
        raise TypeError(f"{field} must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _parse_datetime(value, field):
    if not isinstance(value, str):
        raise TypeError(f"{field} must be an ISO datetime")
    return _validate_datetime(datetime.fromisoformat(value), field)


def _parse_date(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an ISO date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return parsed


def _parse_stage_time(value):
    if value not in STAGES:
        raise ValueError("stage must be one of the frozen four live stages")
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour, minute)


def _strict_bool(value, field):
    if type(value) is not bool:
        raise TypeError(f"{field} must be a bool")
    return value


def _require_text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError(f"{field} is required")
    return value


def _entry_hash(record):
    return hashlib.sha256(_json(record).encode("utf-8")).hexdigest()


def _json(record):
    return canonical_json(record)


class _LedgerLock:
    def __init__(self, path: Path):
        self.path = path.with_suffix(path.suffix + ".lock")
        self.fd = None

    def __enter__(self):
        deadline = time_module.monotonic() + 5
        while True:
            private_path(self.path)
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                return self
            except FileExistsError:
                if time_module.monotonic() > deadline:
                    raise TimeoutError("live acceptance ledger lock timeout")
                time_module.sleep(0.02)

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
