"""Opt-in immutable private stage observations; never backfill a baseline."""

from datetime import date, datetime, time, timedelta
import hashlib

from app.utils.private_storage import (
    canonical_json, private_path, read_private_bytes, strict_json, write_private_exclusive,
)
from app.workflow.stage_evidence import (
    MARKET_TZ, validate_holding_observation, validate_stage_envelope,
)


SCHEMA = "private_stage_record.v1"
STAGE_TIMES = {"global": time(6, 30), "morning": time(9), "midday": time(11, 30),
               "trading": time(14, 30), "closing": time(16, 10)}
MAX_STAGE_DELAY = timedelta(minutes=10)


class StageStore:
    def __init__(self, directory, *, clock=None):
        if clock is not None and not callable(clock):
            raise ValueError("stage clock must be callable")
        self.directory = private_path(directory)
        if any(path.exists() and not path.is_dir() for path in (self.directory, *self.directory.parents)):
            raise ValueError("private stage directory must not be a file")
        self.clock = (lambda: datetime.now(MARKET_TZ)) if clock is None else clock

    def record(self, evidence, *, forecasts=()):
        """Save only the first observation of a stage, using the writer's clock.

        No forecast is inferred from a report, a technical label, or a later
        outcome. Explicit forecasts must be prospective at actual persistence.
        """
        if validate_stage_envelope(evidence):
            raise ValueError("invalid private stage evidence")
        recorded_at = self.clock()
        _validate_window(evidence, recorded_at)
        record = {"schema": SCHEMA, "recorded_at": recorded_at.isoformat(),
                  "evidence": evidence, "forecasts": _prospective_forecasts(forecasts, evidence, recorded_at)}
        record["record_fingerprint"] = _digest(record)
        path = self._path(evidence["trading_date"], evidence["report_kind"])
        try:
            write_private_exclusive(path, canonical_json(record).encode("utf-8"))
        except FileExistsError:
            # A corrupt/half-written first record is not permission to replace it.
            self.load_stage(evidence["trading_date"], evidence["report_kind"])
            return {"status": "already_recorded"}
        return {"status": "stored"}

    def load_morning(self, trading_date):
        record = self.load_stage(trading_date, "morning")
        return record["evidence"] if record is not None else None

    def load_forecasts(self, trading_date):
        forecasts = []
        for kind in ("morning", "midday", "trading"):
            record = self.load_stage(trading_date, kind)
            if record is not None:
                forecasts.extend(record["forecasts"])
        return forecasts

    def _path(self, trading_date, kind):
        if not isinstance(trading_date, str):
            raise ValueError("trading_date must be an ISO date")
        if date.fromisoformat(trading_date).isoformat() != trading_date:
            raise ValueError("trading_date must be an ISO date")
        if not isinstance(kind, str) or kind not in STAGE_TIMES:
            raise ValueError("unsupported stage kind")
        return private_path(self.directory / trading_date / f"{kind}.json")

    def load_stage(self, trading_date, kind):
        """Read and revalidate the immutable first observation of any stage."""
        path = self._path(trading_date, kind)
        try:
            record = strict_json(read_private_bytes(path))
        except FileNotFoundError:
            return None
        if not isinstance(record, dict) or record.get("schema") != SCHEMA:
            raise ValueError("invalid private stage record")
        digest = _digest({key: value for key, value in record.items() if key != "record_fingerprint"})
        if record.get("record_fingerprint") != digest:
            raise ValueError("private stage fingerprint mismatch")
        evidence = record.get("evidence")
        if validate_stage_envelope(evidence):
            raise ValueError("invalid stored stage evidence")
        if evidence["trading_date"] != trading_date or evidence["report_kind"] != kind:
            raise ValueError("private stage record identity mismatch")
        recorded_at = _timestamp(record.get("recorded_at"))
        _validate_window(evidence, recorded_at)
        normalized = _prospective_forecasts(record.get("forecasts"), evidence, recorded_at)
        if normalized != record["forecasts"]:
            raise ValueError("forecast timestamp disagrees with its writer record")
        return record


def _validate_window(evidence, recorded_at):
    if not isinstance(recorded_at, datetime) or recorded_at.utcoffset() is None:
        raise ValueError("stage clock must be timezone-aware")
    start = datetime.combine(date.fromisoformat(evidence["trading_date"]),
                             STAGE_TIMES[evidence["report_kind"]], MARKET_TZ)
    cutoff = datetime.fromisoformat(evidence["cutoff"])
    generated = datetime.fromisoformat(evidence["generated_at"])
    if not start <= cutoff <= generated <= recorded_at <= start + MAX_STAGE_DELAY:
        raise ValueError("private stage write outside real stage window")


def _prospective_forecasts(forecasts, evidence, recorded_at):
    if not isinstance(forecasts, (list, tuple)):
        raise ValueError("forecasts must be an explicit sequence")
    result = []
    for forecast in forecasts:
        if not isinstance(forecast, dict) or forecast.get("explicit") is not True or forecast.get("prospective") is not True:
            raise ValueError("forecast must be explicit and prospective")
        start = _timestamp(forecast.get("start"))
        deadline = _timestamp(forecast.get("deadline"))
        if not recorded_at <= start < deadline:
            raise ValueError("forecast must be recorded before its start")
        if forecast.get("rule_version") != evidence["rule_version"] or forecast.get("cutoff") != evidence["cutoff"]:
            raise ValueError("forecast must use this frozen evidence version")
        target = forecast.get("target")
        if not isinstance(target, dict):
            raise ValueError("forecast target missing")
        matched = [item for item in evidence["holdings"] if all(
            target.get(key) == item.get(key) for key in
            ("account_id", "code", "market", "valuation_mode", "valuation_source", "holding_config_fingerprint")
        )]
        if (len(matched) != 1
                or validate_holding_observation(matched[0], _timestamp(evidence["cutoff"]), evidence["report_kind"])
                or forecast.get("start_price") != matched[0]["price"]):
            raise ValueError("forecast must reference a valid observed starting price")
        if forecast.get("direction") not in ("up", "down", "flat"):
            raise ValueError("forecast direction missing")
        result.append({**forecast, "recorded_at": recorded_at.isoformat()})
    return result


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("stage timestamp must be an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("stage timestamp must be timezone-aware")
        parsed.astimezone(MARKET_TZ)
        return parsed
    except (ValueError, OverflowError):
        raise ValueError("invalid stage timestamp") from None


def _digest(value):
    # Detect accidental mutation only. Anyone with write access can recompute it.
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
