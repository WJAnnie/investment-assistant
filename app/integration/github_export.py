"""Export validated portfolio snapshots for GitHub-to-ChatGPT handoff.

Snapshot Contract V2 enforces the trading-system data-readiness handshake:
a consumer must verify data_cutoff / market_date / analysis_state / bar_status
before treating a snapshot as analyzable evidence (rules R17/R18). Every
check fails closed: ambiguous, future, unprovenanced, or inconsistent data
is rejected instead of being repackaged as analyzable evidence.
"""

import argparse
from datetime import datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo

from app.market.factory import create_global_market_providers
from app.report.portfolio import STAGE_HEADINGS
from app.utils.redaction import is_sensitive_key, redact_secrets
from app.utils.serialization import to_jsonable
from app.workflow.portfolio import GLOBAL_MARKET_STAGES, run_portfolio_report


SCHEMA_VERSION = 2
RULE_VERSION = "8.1"
DATA_CLASSIFICATION = "private_account_data"
MARKET_TIMEZONE = "Asia/Shanghai"
SNAPSHOT_NAMES = tuple(STAGE_HEADINGS)
ANALYSIS_STATES = frozenset({"READY", "DEGRADED", "NOT_READY", "FAILED"})
REPORT_KIND_CUTOFFS = {
    "global": "06:30",
    "morning": "09:00",
    "midday": "11:30",
    "trading": "14:30",
    "closing": "16:10",
}
MAX_CURRENT_SNAPSHOT_AGE = timedelta(minutes=30)
_PRODUCER_FIELDS = ("repository", "workflow", "run_id", "source_sha")


def build_snapshot(report_kind, result, *, now=None, environ=None):
    """Build the versioned, provenance-carrying ChatGPT data contract."""
    if report_kind not in STAGE_HEADINGS:
        raise ValueError(f"unsupported report_kind: {report_kind}")
    if not isinstance(result, dict):
        raise TypeError("result must be a mapping")

    current_time = now or datetime.now(ZoneInfo(MARKET_TIMEZONE))
    if current_time.tzinfo is None:
        raise ValueError("now must be timezone-aware for cutoff gating")
    environment = environ if environ is not None else os.environ
    analysis = result.get("analysis")
    if analysis is None:
        analysis = {}
    if not isinstance(analysis, dict):
        raise ValueError("analysis must be a mapping")
    coverage = _coverage_counts(analysis.get("coverage"))
    # data_cutoff is the market-time anchor a consumer must compare against
    # its own expected slot; generated_at only records pipeline completion.
    local = current_time.astimezone(ZoneInfo(MARKET_TIMEZONE))
    cutoff_time = REPORT_KIND_CUTOFFS.get(report_kind)
    if cutoff_time is None:
        raise ValueError(f"unsupported report_kind: {report_kind}")
    hour, minute = (int(part) for part in cutoff_time.split(":", 1))
    data_cutoff = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    market_date = data_cutoff.date().isoformat()
    # Readiness gate: a run that finished before the data cutoff cannot
    # contain the cutoff evidence and must never claim an analyzable state.
    # The snapshot is still published (fail-closed, not fail-silent) with an
    # explicit NOT_READY state so the consumer sees the gate reason instead
    # of fabricated freshness.
    gate_errors = []
    if current_time < data_cutoff:
        gate_errors.append(
            "readiness-gate: generated_at is earlier than data_cutoff; "
            f"expected data no later than {data_cutoff.isoformat()} "
            f"(GITHUB-EXPORT-001)"
        )
    result = dict(result)
    if gate_errors:
        result["errors"] = list(result.get("errors") or ()) + gate_errors
        if result.get("status") != "failed":
            result["status"] = "partial"
    valuations = result.get("valuations") or {}
    failed_codes = sorted(
        str(code)
        for code, valuation in valuations.items()
        if getattr(valuation, "freshness", None) == "failed"
        or (isinstance(valuation, dict) and valuation.get("freshness") == "failed")
    )
    stale_codes = sorted(
        str(code)
        for code, valuation in valuations.items()
        if getattr(valuation, "freshness", None) in {"stale", "lagged"}
        or (
            isinstance(valuation, dict)
            and valuation.get("freshness") in {"stale", "lagged"}
        )
    )
    total = coverage["total"]
    ready = coverage["ready"]
    has_quality_gap = bool(
        result.get("errors")
        or failed_codes
        or stale_codes
        or (total > 0 and ready < total)
    )
    if result.get("status") != "failed" and has_quality_gap:
        result["status"] = "partial"
    analysis_state = _analysis_state(result, coverage)
    if gate_errors and analysis_state not in ("NOT_READY", "FAILED"):
        analysis_state = "NOT_READY"
    bar_status = _bar_status(analysis)

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "data_classification": DATA_CLASSIFICATION,
        "generated_at": current_time.isoformat(),
        "market_date": market_date,
        "data_cutoff": data_cutoff.isoformat(),
        "execution_status": result.get("status", "failed"),
        "analysis_state": analysis_state,
        "market_timezone": MARKET_TIMEZONE,
        "report_kind": report_kind,
        "report_heading": STAGE_HEADINGS[report_kind],
        "status": result.get("status", "failed"),
        "producer": _producer_metadata(environment),
        "quality": {
            "ready": (
                analysis_state == "READY"
                and result.get("status") == "completed"
            ),
            "missing_sources": _missing_sources(analysis),
            "stale_sources": stale_codes,
            "analysis_ready": coverage["ready"],
            "analysis_total": coverage["total"],
            "analysis_unavailable": coverage["unavailable"],
            "failed_valuation_codes": failed_codes,
            "errors": list(result.get("errors") or ()),
        },
        "result": to_jsonable(result),
    }
    if bar_status:
        snapshot["bar_status"] = to_jsonable(bar_status)
    # Redacting a key substring must not disguise a forbidden credential field.
    if any(_find_sensitive_keys(snapshot)):
        raise ValueError("snapshot contains sensitive keys")
    snapshot = redact_secrets(snapshot, environment)
    validate_snapshot(snapshot, environ=environment)
    return snapshot


def _producer_metadata(environment):
    """Map GitHub Actions context into auditable producer provenance."""
    return {
        "type": "github_actions",
        "repository": environment.get("GITHUB_REPOSITORY", ""),
        "workflow": environment.get("GITHUB_WORKFLOW", ""),
        "run_id": environment.get("GITHUB_RUN_ID", ""),
        "run_attempt": environment.get("GITHUB_RUN_ATTEMPT", ""),
        "source_sha": environment.get("GITHUB_SHA", ""),
        "event_name": environment.get("GITHUB_EVENT_NAME", ""),
    }


def _coverage_counts(coverage):
    """Validate counters without coercing booleans, fractions or missing evidence."""
    if coverage is None:
        coverage = {}
    if not isinstance(coverage, dict):
        raise ValueError("analysis coverage must be a mapping")
    counts = {name: coverage.get(name, 0) for name in ("ready", "total", "unavailable")}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("analysis coverage counts must be nonnegative integers")
    if counts["ready"] + counts["unavailable"] != counts["total"]:
        raise ValueError("analysis coverage ready + unavailable must equal total")
    return counts


def _analysis_state(result, coverage):
    """Derive the four-state readiness contract from pipeline outcomes."""
    status = result.get("status", "failed")
    if status == "failed":
        return "FAILED"
    total = coverage["total"]
    ready = coverage["ready"]
    if total <= 0 or ready == 0:
        return "NOT_READY"
    if result.get("full_analysis_ready") is False:
        return "DEGRADED"
    if ready < total:
        return "DEGRADED"
    return "READY"


# Per-timeframe bar states ordered from most to least trustworthy. Anything
# outside this mapping is treated as an unknown value and never aggregated
# into CLOSED, so a producer typo cannot fabricate market closure.
_BAR_STATES = ("CLOSED", "FORMING", "UNKNOWN", "MISSING")
_BAR_STATE_RANK = {state: rank for rank, state in enumerate(_BAR_STATES)}


def _bar_status(analysis):
    """Aggregate per-holding bar status without inventing missing data.

    Aggregation is worst-case per timeframe over the union of cycles any
    holding reported: a holding that reports (or fails to report) a less
    trustworthy state downgrades that cycle's aggregate. Absent evidence
    counts as MISSING and unrecognized values as UNKNOWN, so an unknown
    enum from a future producer can never surface as CLOSED, unavailable
    holdings are never silently skipped, and multi-cycle statuses are
    merged per cycle instead of being flattened or dropped.
    """
    holdings = []
    for item in analysis.get("items") or ():
        if not isinstance(item, dict):
            holdings.append(None)
            continue
        if item.get("status") is not None and item.get("status") != "ready":
            # Unavailable holdings carry no bar evidence at all.
            holdings.append(None)
            continue
        raw = item.get("bar_status")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            # A ready holding without bar evidence has no bar evidence.
            holdings.append(None)
            continue
        cycles = raw if isinstance(raw, dict) else {"daily": raw}
        states = {
            str(cycle): _normalize_bar_state(value)
            for cycle, value in cycles.items()
        }
        holdings.append(states or None)
    if not holdings:
        # No holdings at all: closure can neither be confirmed nor denied.
        return {"daily": "UNKNOWN"}
    all_cycles = set()
    for states in holdings:
        if states:
            all_cycles.update(states)
    all_cycles.add("daily")
    aggregate = {}
    for cycle in sorted(all_cycles):
        worst = None
        for states in holdings:
            if not states or cycle not in states:
                # The holding reported no evidence for this cycle.
                state = "MISSING"
            else:
                state = states[cycle]
            worst = _worse(worst, state)
        aggregate[cycle] = worst
    return aggregate


def _normalize_bar_state(value):
    """Map a raw bar value onto the contract states, unknowns included."""
    if isinstance(value, dict):
        # Nested mappings have no defined aggregate; surface as unknown.
        return "UNKNOWN"
    if isinstance(value, bool):
        return "UNKNOWN"
    if value is None:
        return "MISSING"
    text = str(value).strip()
    if not text:
        return "MISSING"
    if text in _BAR_STATE_RANK:
        return text
    if text in ("unavailable", "missing", "failed"):
        return "MISSING"
    if text in ("complete", "closed", "completed_only", "forming"):
        # Producer-level vocabulary mapped onto the contract states.
        return "CLOSED" if text in ("complete", "closed") else "FORMING"
    return "UNKNOWN"


def _worse(current, incoming):
    """Combine two per-cycle states by worst-case ordering."""
    if current is None:
        return incoming
    return max(
        (current, incoming),
        key=lambda state: _BAR_STATE_RANK.get(state, _BAR_STATE_RANK["UNKNOWN"]),
    )


def _missing_sources(analysis):
    """List data-limit keys without fabricating availability."""
    limits = analysis.get("data_limits") or {}
    if not isinstance(limits, dict):
        return []
    return sorted(
        str(key) for key, value in limits.items()
        if value in ("unavailable", "missing", "failed")
    )


def _private_output_path(output, environ=None):
    """Fail before account access if the runtime or output is not private.

    CI visibility must come from the workflow's authenticated GitHub API
    preflight, not from a caller-selected public/private export switch.
    Local .private/ isolation prevents accidental Git commits; it does not
    provide encryption or replace filesystem access controls.
    """
    environment = environ if environ is not None else os.environ
    if "GITHUB_ACTIONS" in environment:
        if (
            environment.get("GITHUB_ACTIONS") != "true"
            or environment.get("GITHUB_REPOSITORY_VISIBILITY") != "private"
        ):
            raise ValueError(
                "private account exports require a verified private GitHub repository"
            )
        return Path(output).resolve()

    destination = Path(output).resolve()
    source_root = Path(__file__).resolve().parents[2]
    message = "private export output must be in .private/ or outside Git worktrees"
    if destination.is_relative_to(source_root) and not destination.is_relative_to(
        source_root / ".private"
    ):
        raise ValueError(message)
    # Resolve first: '..' and symlinks must not escape the ignored directory
    # into tracked source paths. Other repositories have unknown visibility.
    for ancestor in destination.parents:
        if (ancestor / ".git").exists():
            if ancestor != source_root:
                raise ValueError(message)
            break
    return destination


def collect_snapshot(report_kind, output, *, runner=run_portfolio_report, now=None, environ=None,
                     global_provider=None, treasury_fallback=None):
    """Run the portfolio workflow once and save a private account snapshot."""
    output = _private_output_path(output, environ)
    current_time = now or datetime.now(ZoneInfo(MARKET_TIMEZONE))
    options = {}
    if global_provider is not None:
        options["global_provider"] = global_provider
    if treasury_fallback is not None:
        options["treasury_fallback"] = treasury_fallback
    result = runner(report_kind=report_kind, notifier=None, now=current_time, **options)
    snapshot = build_snapshot(
        report_kind,
        result,
        now=current_time,
        environ=environ,
    )
    write_json_atomic(output, snapshot)
    return snapshot


def validate_snapshot(snapshot, *, environ=None):
    """Reject malformed, unprovenanced, or secret-bearing handoff documents."""
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a JSON object")
    # Check secrets before parsing untrusted fields: parser errors can echo
    # their input. Never include raw field paths or secret values in errors.
    if any(_find_sensitive_keys(snapshot)):
        raise ValueError("snapshot contains sensitive keys")
    if redact_secrets(snapshot, environ) != snapshot:
        raise ValueError("snapshot contains sensitive values")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported snapshot schema_version")
    if snapshot.get("rule_version") != RULE_VERSION:
        raise ValueError("unsupported snapshot rule_version")
    if snapshot.get("data_classification") != DATA_CLASSIFICATION:
        raise ValueError("snapshot data_classification must be private_account_data")
    report_kind = snapshot.get("report_kind")
    if report_kind not in STAGE_HEADINGS:
        raise ValueError("snapshot has an invalid report_kind")
    if snapshot.get("status") not in {"completed", "partial", "failed"}:
        raise ValueError("snapshot has an invalid status")
    if snapshot.get("execution_status") not in {"completed", "partial", "failed"}:
        raise ValueError("snapshot has an invalid execution_status")
    if snapshot.get("execution_status") != snapshot.get("status"):
        raise ValueError("snapshot status/execution_status mismatch")
    if snapshot.get("analysis_state") not in ANALYSIS_STATES:
        raise ValueError("snapshot has an invalid analysis_state")
    market_date = str(snapshot.get("market_date", ""))
    if not market_date:
        raise ValueError("snapshot market_date is required")
    generated_at = datetime.fromisoformat(str(snapshot.get("generated_at", "")))
    if generated_at.tzinfo is None:
        raise ValueError("snapshot generated_at must include a timezone")
    result = snapshot.get("result")
    if not isinstance(result, dict) or result.get("report_kind") != report_kind:
        raise ValueError("snapshot result/report_kind mismatch")
    data_cutoff = datetime.fromisoformat(str(snapshot.get("data_cutoff", "")))
    if data_cutoff.tzinfo is None:
        raise ValueError("snapshot data_cutoff must include a timezone")
    if data_cutoff.date().isoformat() != market_date:
        raise ValueError("snapshot data_cutoff/market_date mismatch")
    expected_cutoff = datetime.combine(
        data_cutoff.date(),
        time(*map(int, REPORT_KIND_CUTOFFS[report_kind].split(":"))),
        tzinfo=ZoneInfo(MARKET_TIMEZONE),
    )
    if data_cutoff.astimezone(ZoneInfo(MARKET_TIMEZONE)) != expected_cutoff:
        raise ValueError(
            "snapshot data_cutoff does not match report_kind cutoff "
            f"{REPORT_KIND_CUTOFFS[report_kind]}"
        )
    if generated_at < data_cutoff and snapshot.get("analysis_state") in (
        "READY",
        "DEGRADED",
    ):
        # The pipeline cannot have finished before the evidence cutoff it
        # claims; an analyzable state here would let a premature run
        # masquerade as the cutoff-time evidence slot. Explicitly
        # NOT_READY/FAILED snapshots may be published for diagnosis.
        raise ValueError(
            "snapshot generated_at is earlier than data_cutoff but claims "
            "an analyzable analysis_state (GITHUB-EXPORT-001)"
        )
    bar_status = snapshot.get("bar_status", {})
    if not isinstance(bar_status, dict) or not bar_status:
        raise ValueError("snapshot bar_status must be a non-empty mapping")
    for cycle, state in bar_status.items():
        if not isinstance(cycle, str) or not cycle.strip():
            raise ValueError("snapshot bar_status has an invalid cycle")
        if state not in _BAR_STATES:
            raise ValueError(
                f"snapshot bar_status[{cycle}] has an unknown state: {state!r}"
            )
    producer = snapshot.get("producer")
    if not isinstance(producer, dict):
        raise ValueError("snapshot producer must be an object")
    for field in _PRODUCER_FIELDS:
        value = producer.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"snapshot producer.{field} must be a non-empty string"
            )
    if snapshot.get("execution_status") == "failed" and snapshot.get(
        "analysis_state"
    ) in ("READY", "DEGRADED"):
        # A failed execution cannot simultaneously claim analyzable output;
        # partial runs may legitimately surface as READY/DEGRADED with
        # degraded confidence.
        raise ValueError(
            "snapshot analysis_state is not consistent with execution_status"
        )
    if result.get("status") != snapshot["status"]:
        raise ValueError("snapshot result/status mismatch")
    analysis = result.get("analysis")
    if analysis is None:
        analysis = {}
    if not isinstance(analysis, dict):
        raise ValueError("snapshot analysis must be a mapping")
    if bar_status != _bar_status(analysis):
        raise ValueError("snapshot bar_status/analysis mismatch")
    coverage = _coverage_counts(analysis.get("coverage"))
    quality = snapshot.get("quality")
    if not isinstance(quality, dict):
        raise ValueError("snapshot quality coverage is required")
    for name, count in coverage.items():
        public_count = quality.get(f"analysis_{name}")
        if type(public_count) is not int or public_count != count:
            raise ValueError("snapshot quality/analysis coverage mismatch")
    expected_state = _analysis_state(result, coverage)
    if generated_at < data_cutoff and expected_state != "FAILED":
        expected_state = "NOT_READY"
    if snapshot["analysis_state"] != expected_state:
        raise ValueError("snapshot analysis_state/coverage mismatch")
    if snapshot["execution_status"] == "completed" and expected_state in ("READY", "DEGRADED"):
        # Diagnostic partial/failed exports may lack item arrays. A completed
        # analyzable release must prove its counters with actual item evidence.
        items = analysis.get("items")
        if not isinstance(items, list) or len(items) != coverage["total"]:
            raise ValueError("completed snapshot lacks matching item coverage evidence")
        statuses = [item.get("status") if isinstance(item, dict) else None for item in items]
        if any(statuses.count(state) != coverage[state] for state in ("ready", "unavailable")):
            raise ValueError("completed snapshot lacks matching item coverage evidence")
    return snapshot


def validate_snapshot_file(path, *, environ=None):
    with Path(path).open("r", encoding="utf-8") as handle:
        return validate_snapshot(json.load(handle), environ=environ)


def build_manifest(latest_dir, current_file, output, *, now=None, environ=None):
    """Describe all latest snapshots and identify the current event payload.

    The current payload and the same report_kind's latest entry must be the
    identical bytes: a diverging pair means the just-published snapshot and
    the durable per-kind archive disagree, and consumers could read either
    as "the" evidence. Legacy snapshots that predate the current schema are
    recorded as superseded and preserved instead of blocking or misleading.
    """
    output = _private_output_path(output, environ)
    current_time = now or datetime.now(ZoneInfo(MARKET_TIMEZONE))
    if current_time.tzinfo is None:
        raise ValueError("now must be timezone-aware for manifest freshness gating")
    latest_path = Path(latest_dir)
    current_path = Path(current_file)
    current = validate_snapshot_file(current_path, environ=environ)
    current_raw = current_path.read_bytes()
    current_digest = hashlib.sha256(current_raw).hexdigest()
    entries = {}
    superseded = []
    for report_kind in SNAPSHOT_NAMES:
        path = latest_path / f"{report_kind}.json"
        if not path.exists():
            continue
        try:
            snapshot = validate_snapshot_file(path, environ=environ)
        except ValueError as exc:
            # Only an explicitly identified V1 document may be superseded.
            # A malformed, tampered, or invalid V2 file must remain on disk and
            # fail the publish so corruption cannot be hidden by deletion.
            try:
                with path.open("r", encoding="utf-8") as handle:
                    legacy_candidate = json.load(handle)
            except (OSError, ValueError):
                raise ValueError(
                    f"invalid latest snapshot {path.name}; file was preserved"
                ) from exc
            if not isinstance(legacy_candidate, dict) or legacy_candidate.get("schema_version") != 1:
                raise ValueError(
                    f"invalid latest snapshot {path.name}; file was preserved"
                ) from exc
            superseded.append(
                {
                    "path": f"data/chatgpt/latest/{report_kind}.json",
                    "reason": f"legacy schema_version 1 superseded; file preserved: {exc}",
                }
            )
            continue
        if snapshot["report_kind"] != report_kind:
            raise ValueError(f"snapshot filename/report_kind mismatch: {path.name}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if report_kind == current["report_kind"] and digest != current_digest:
            raise ValueError(
                "manifest current/latest content mismatch for "
                f"{report_kind}: the current payload and the latest archive "
                "must be identical bytes"
            )
        entries[report_kind] = {
            "path": f"data/chatgpt/latest/{report_kind}.json",
            "data_classification": DATA_CLASSIFICATION,
            "market_date": snapshot["market_date"],
            "data_cutoff": snapshot["data_cutoff"],
            "generated_at": snapshot["generated_at"],
            "execution_status": snapshot["execution_status"],
            "analysis_state": snapshot["analysis_state"],
            "rule_version": snapshot["rule_version"],
            "status": snapshot["status"],
            "run_id": snapshot["producer"].get("run_id", ""),
            "source_sha": snapshot["producer"].get("source_sha", ""),
            "sha256": digest,
            "git_blob_sha": _git_blob_sha(raw),
        }
    if current["report_kind"] not in entries:
        # Defensive: the publisher must have copied current into latest for
        # the same report_kind before building the manifest.
        raise ValueError(
            "manifest is missing the latest entry for the current "
            f"report_kind {current['report_kind']}"
        )
    publishable, not_ready_reason = _publishability(current, current_time)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "data_classification": DATA_CLASSIFICATION,
        "updated_at": current_time.isoformat(),
        "publishable": publishable,
        "data_not_ready_reason": not_ready_reason,
        "current": {
            "path": "data/chatgpt/current.json",
            "data_classification": DATA_CLASSIFICATION,
            "report_kind": current["report_kind"],
            "market_date": current["market_date"],
            "data_cutoff": current["data_cutoff"],
            "generated_at": current["generated_at"],
            "execution_status": current["execution_status"],
            "analysis_state": current["analysis_state"],
            "rule_version": current["rule_version"],
            "status": current["status"],
            "run_id": current["producer"].get("run_id", ""),
            "sha256": current_digest,
            "git_blob_sha": _git_blob_sha(current_raw),
        },
        "latest": entries,
        "superseded": superseded,
    }
    write_json_atomic(output, manifest)
    return manifest


def _publishability(snapshot, now):
    """Decide whether the manifest advertises an analyzable current payload.

    NOT_READY / FAILED / partial executions stay published for diagnosis,
    but the manifest must not let CI or consumers treat them as a
    successful, analyzable release.
    """
    analysis_state = snapshot.get("analysis_state")
    execution_status = snapshot.get("execution_status")
    if execution_status == "failed":
        return False, f"current execution_status is failed: {analysis_state}"
    if analysis_state == "NOT_READY":
        return False, "current analysis_state is NOT_READY"
    if analysis_state == "FAILED":
        return False, "current analysis_state is FAILED"
    if analysis_state not in ("READY", "DEGRADED"):
        return False, f"current analysis_state is unrecognized: {analysis_state}"
    if execution_status != "completed":
        return False, (
            f"current execution_status is {execution_status}; "
            "only completed runs are analyzable"
        )
    generated_at = datetime.fromisoformat(str(snapshot.get("generated_at", "")))
    if generated_at > now:
        return False, "current snapshot generated_at is in the future"
    snapshot_age = now - generated_at
    if snapshot_age > MAX_CURRENT_SNAPSHOT_AGE:
        return False, (
            "current snapshot is stale: generated_at is more than "
            f"{int(MAX_CURRENT_SNAPSHOT_AGE.total_seconds() // 60)} minutes old"
        )
    local_now = now.astimezone(ZoneInfo(MARKET_TIMEZONE))
    if snapshot.get("market_date") != local_now.date().isoformat():
        return False, "current snapshot market_date does not match the manifest date"
    bar_status = snapshot.get("bar_status") or {}
    missing_cycles = sorted(
        cycle
        for cycle, state in bar_status.items()
        if state in {"MISSING", "UNKNOWN"}
    )
    if missing_cycles:
        return False, (
            "current bar_status lacks evidence for cycles: "
            + ", ".join(missing_cycles)
        )
    if snapshot.get("report_kind") == "closing":
        open_cycles = sorted(
            f"{cycle}={state}"
            for cycle, state in bar_status.items()
            if state != "CLOSED"
        )
        if open_cycles:
            return False, (
                "closing bar_status must be CLOSED for all cycles: "
                + ", ".join(open_cycles)
            )
    return True, None


def _git_blob_sha(raw):
    """Return the object ID exposed by GitHub's contents API for file bytes."""
    header = f"blob {len(raw)}\0".encode("ascii")
    return hashlib.sha1(header + raw).hexdigest()


def write_json_atomic(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _find_sensitive_keys(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if is_sensitive_key(key):
                yield path
            yield from _find_sensitive_keys(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _find_sensitive_keys(item, f"{prefix}[{index}]")


def build_parser():
    parser = argparse.ArgumentParser(description="Private GitHub-to-ChatGPT portfolio data exporter")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="collect one private portfolio snapshot")
    collect.add_argument("--report-kind", required=True, choices=SNAPSHOT_NAMES)
    collect.add_argument("--output", required=True)

    validate = commands.add_parser("validate", help="validate one snapshot")
    validate.add_argument("--input", required=True)

    manifest = commands.add_parser("manifest", help="build the latest-snapshot manifest")
    manifest.add_argument("--latest-dir", required=True)
    manifest.add_argument("--current", required=True)
    manifest.add_argument("--output", required=True)

    gate = commands.add_parser(
        "gate",
        help="fail unless the manifest advertises an analyzable current payload",
    )
    gate.add_argument("--manifest", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "collect":
        _private_output_path(args.output)
        global_provider = None
        treasury_fallback = None
        if args.report_kind in GLOBAL_MARKET_STAGES:
            global_provider, treasury_fallback = create_global_market_providers()
        snapshot = collect_snapshot(
            args.report_kind,
            args.output,
            global_provider=global_provider,
            treasury_fallback=treasury_fallback,
        )
        # Exit codes distinguish outcomes for CI: a failed pipeline is 1,
        # a partial/NOT_READY run that must not be treated as a successful
        # publishable release is 2.
        if snapshot["status"] == "failed":
            return 1
        if snapshot["status"] == "partial":
            return 2
        return 0
    if args.command == "validate":
        validate_snapshot_file(args.input)
        return 0
    if args.command == "manifest":
        build_manifest(args.latest_dir, args.current, args.output)
        return 0
    with Path(args.manifest).open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict) or manifest.get("data_classification") != DATA_CLASSIFICATION:
        print(
            "PRIVATE_DATA_REQUIRED: manifest data_classification must be private_account_data",
            file=sys.stderr,
        )
        return 4
    publishable = manifest.get("publishable")
    if publishable is not True:
        reason = manifest.get("data_not_ready_reason") or "publishable flag is missing"
        print(
            f"DATA_NOT_READY: {reason}; the published latest snapshot must "
            "not be analyzed as a successful release",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
