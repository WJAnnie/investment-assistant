import hashlib
from copy import deepcopy
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import quote
from zoneinfo import ZoneInfo

import yaml

from app.integration.github_export import (
    build_manifest,
    build_snapshot,
    collect_snapshot,
    main,
    validate_snapshot,
)


NOW = datetime(2026, 9, 11, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
EARLY_NOW = datetime(2026, 9, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
ENVIRONMENT = {
    "GITHUB_REPOSITORY": "WJAnnie/investment-assistant",
    "GITHUB_WORKFLOW": "Publish ChatGPT market data",
    "GITHUB_RUN_ID": "12345",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_SHA": "abc123",
    "GITHUB_EVENT_NAME": "workflow_dispatch",
}


def _result(report_kind="closing", status="completed"):
    return {
        "status": status,
        "report_kind": report_kind,
        "snapshot": {"total_assets": Decimal("517773.56")},
        "valuations": {
            "601888": {"freshness": "fresh"},
            "HK.HSTECH": {"freshness": "failed"},
        },
        "analysis": {
            "coverage": {"ready": 8, "total": 13, "unavailable": 5},
            "data_limits": {
                "fundamental": "unavailable",
                "industry": "configured_labels_only",
                "news": "unavailable",
            },
        },
        "report": "A账户\nB账户\n全部账户",
        "notified": False,
        "errors": ["HK.HSTECH: provider offline"],
    }


def _ready_result(report_kind="closing"):
    result = _result(report_kind, status="completed")
    result["valuations"] = {"601888": {"freshness": "fresh"}}
    result["analysis"]["coverage"] = {
        "ready": 1,
        "total": 1,
        "unavailable": 0,
    }
    result["analysis"]["items"] = [
        {"status": "ready", "bar_status": "complete"}
    ]
    result["errors"] = []
    return result


class GitHubExportTests(unittest.TestCase):
    def test_builds_versioned_snapshot_with_quality_and_provenance(self):
        snapshot = build_snapshot(
            "closing",
            _result(),
            now=NOW,
            environ=ENVIRONMENT,
        )

        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(snapshot["rule_version"], "8.1")
        self.assertEqual(snapshot["generated_at"], "2026-09-11T16:30:00+08:00")
        self.assertEqual(snapshot["market_date"], "2026-09-11")
        self.assertEqual(
            snapshot["data_cutoff"], "2026-09-11T16:10:00+08:00"
        )
        self.assertEqual(snapshot["execution_status"], "partial")
        self.assertEqual(snapshot["analysis_state"], "DEGRADED")
        self.assertEqual(snapshot["quality"]["ready"], False)
        self.assertEqual(
            snapshot["quality"]["missing_sources"],
            ["fundamental", "news"],
        )
        self.assertEqual(snapshot["producer"]["run_id"], "12345")
        self.assertEqual(snapshot["quality"]["analysis_ready"], 8)
        self.assertEqual(snapshot["quality"]["analysis_unavailable"], 5)
        self.assertEqual(snapshot["quality"]["failed_valuation_codes"], ["HK.HSTECH"])
        self.assertEqual(snapshot["result"]["snapshot"]["total_assets"], "517773.56")

    def test_snapshot_lists_stale_and_lagged_valuation_sources(self):
        result = _result()
        result["valuations"].update({
            "STALE": {"freshness": "stale"},
            "LAGGED": {"freshness": "lagged"},
        })

        snapshot = build_snapshot(
            "closing", result, now=NOW, environ=ENVIRONMENT
        )

        self.assertEqual(snapshot["quality"]["stale_sources"], ["LAGGED", "STALE"])
        self.assertEqual(snapshot["execution_status"], "partial")

    def test_quality_ready_is_false_when_stale_data_forces_partial_execution(self):
        result = _ready_result()
        result["valuations"]["STALE"] = {"freshness": "stale"}

        snapshot = build_snapshot(
            "closing", result, now=NOW, environ=ENVIRONMENT
        )

        self.assertEqual(snapshot["analysis_state"], "READY")
        self.assertEqual(snapshot["execution_status"], "partial")
        self.assertFalse(snapshot["quality"]["ready"])

    def test_collect_writes_valid_utf8_json_without_notifier(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return _result(kwargs["report_kind"])

        with TemporaryDirectory() as directory:
            output = Path(directory) / "current.json"
            snapshot = collect_snapshot(
                "trading",
                output,
                runner=runner,
                now=NOW,
                environ=ENVIRONMENT,
            )
            loaded = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(calls, [{"report_kind": "trading", "notifier": None, "now": NOW}])
        self.assertEqual(loaded, snapshot)
        self.assertIn("A账户", loaded["result"]["report"])

    def test_build_snapshot_rejects_naive_now_for_cutoff_gating(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            build_snapshot(
                "trading",
                _ready_result("trading"),
                now=datetime(2026, 9, 11, 14, 30),
                environ=ENVIRONMENT,
            )

    def test_build_snapshot_accepts_exact_cutoff_without_premature_gate(self):
        cutoff = datetime(
            2026, 9, 11, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")
        )

        snapshot = build_snapshot(
            "trading", _ready_result("trading"), now=cutoff, environ=ENVIRONMENT
        )

        self.assertEqual(snapshot["generated_at"], snapshot["data_cutoff"])
        self.assertEqual(snapshot["execution_status"], "completed")
        self.assertEqual(snapshot["analysis_state"], "READY")

    def test_closing_snapshot_waits_for_unified_ah_cutoff(self):
        before_hk_close = datetime(
            2026, 9, 11, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai")
        )

        snapshot = build_snapshot(
            "closing",
            _ready_result("closing"),
            now=before_hk_close,
            environ=ENVIRONMENT,
        )

        self.assertEqual(snapshot["data_cutoff"], "2026-09-11T16:10:00+08:00")
        self.assertEqual(snapshot["analysis_state"], "NOT_READY")
        self.assertEqual(snapshot["execution_status"], "partial")

    def test_rejects_sensitive_keys_and_result_mismatch(self):
        snapshot = build_snapshot("closing", _result(), now=NOW, environ=ENVIRONMENT)
        snapshot["result"]["feishu_app_secret"] = "do-not-publish"
        with self.assertRaisesRegex(ValueError, "sensitive keys"):
            validate_snapshot(snapshot)

        mismatch = build_snapshot("closing", _result(), now=NOW, environ=ENVIRONMENT)
        mismatch["result"]["report_kind"] = "trading"
        with self.assertRaisesRegex(ValueError, "mismatch"):
            validate_snapshot(mismatch)

    def test_public_snapshot_redacts_credentials_everywhere_without_mutating_input(self):
        secret = "fixture-only/token+secret=42"
        environment = {**ENVIRONMENT, "HITHINK_FINANCE_API_KEY": secret}
        result = _result()
        detail = f"provider offline: {secret}; encoded={quote(secret, safe='')}"
        result["errors"] = [detail]
        result["valuations"]["HK.HSTECH"]["error"] = detail
        result["analysis"]["benchmark"] = {"status": "unavailable", "reason": detail}
        result["report"] = f"人工复核 WAIT: {detail}"
        original = deepcopy(result)

        snapshot = build_snapshot("closing", result, now=NOW, environ=environment)
        payload = json.dumps(snapshot, ensure_ascii=False)

        self.assertNotIn(secret, payload)
        self.assertNotIn(quote(secret, safe=""), payload)
        self.assertIn("[REDACTED]", payload)
        self.assertIn("provider offline", payload)
        self.assertIn("WAIT", snapshot["result"]["report"])
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(result, original)

    def test_public_snapshot_redacts_inline_credentials_without_environment_values(self):
        result = _result()
        result["errors"] = [
            "HTTP failure https://data.example.test/quote?api_key=fixture-query-key&symbol=600000",
            'Authorization: Bearer fixture-bearer-token',
            '{"app_secret": "fixture secret with spaces"}',
            "https://open.feishu.cn/open-apis/bot/v2/hook/fixture-webhook-id",
            "https://fixture-user:fixture-password@data.example.test/api",
        ]

        snapshot = build_snapshot("closing", result, now=NOW, environ=ENVIRONMENT)
        payload = json.dumps(snapshot, ensure_ascii=False)

        for value in (
            "fixture-query-key", "fixture-bearer-token",
            "fixture secret with spaces", "fixture-webhook-id",
            "fixture-user", "fixture-password",
        ):
            with self.subTest(value=value):
                self.assertNotIn(value, payload)
        self.assertIn("symbol=600000", payload)

    def test_validator_rejects_secret_values_without_echoing_them(self):
        secret = "fixture-validator-secret-73"
        snapshot = build_snapshot("closing", _result(), now=NOW, environ=ENVIRONMENT)
        snapshot["result"]["report"] = f"provider detail {secret}"

        with patch.dict(os.environ, {"TUSHARE_TOKEN": secret}):
            with self.assertRaisesRegex(ValueError, "sensitive") as caught:
                validate_snapshot(snapshot)
        self.assertNotIn(secret, str(caught.exception))

    def test_redaction_cannot_hide_sensitive_field_names_from_validation(self):
        result = _ready_result()
        result["unexpected"] = {"client_secret": "fixture-unconfigured-credential"}
        environment = {**ENVIRONMENT, "PASSWORD": "secret"}

        with self.assertRaisesRegex(ValueError, "sensitive keys"):
            build_snapshot("closing", result, now=NOW, environ=environment)

    def test_secret_check_precedes_parser_errors_that_could_echo_input(self):
        secret = "fixture-timestamp-secret"
        snapshot = build_snapshot("closing", _result(), now=NOW, environ=ENVIRONMENT)
        snapshot["generated_at"] = secret
        with self.assertRaisesRegex(ValueError, "sensitive") as caught:
            validate_snapshot(snapshot, environ={"API_KEY": secret})
        self.assertNotIn(secret, str(caught.exception))

    def test_validator_rejects_generic_secret_keys_and_inline_credentials(self):
        for key in ("api_key", "HITHINK_FINANCE_API_KEY", "Authorization", "client_secret"):
            with self.subTest(key=key):
                snapshot = build_snapshot("closing", _result(), now=NOW, environ=ENVIRONMENT)
                snapshot["result"]["unexpected"] = {key: "fixture-only-value"}
                with self.assertRaisesRegex(ValueError, "sensitive keys"):
                    validate_snapshot(snapshot)
        snapshot = build_snapshot("closing", _result(), now=NOW, environ=ENVIRONMENT)
        snapshot["quality"]["errors"] = ["request failed: access_token=fixture-unredacted-token"]
        with self.assertRaisesRegex(ValueError, "sensitive"):
            validate_snapshot(snapshot)

    def test_malformed_analysis_item_is_not_silently_omitted_from_bar_evidence(self):
        result = _ready_result("trading")
        result["status"] = "partial"
        result["analysis"]["items"].append(None)
        result["analysis"]["coverage"] = {"ready": 2, "total": 2, "unavailable": 0}

        snapshot = build_snapshot("trading", result, now=NOW, environ=ENVIRONMENT)

        self.assertEqual(snapshot["bar_status"], {"daily": "MISSING"})

    def test_coverage_counts_must_be_nonnegative_integers(self):
        for field in ("ready", "total", "unavailable"):
            for invalid in (None, True, -1, "1", 1.5, float("nan"), float("inf")):
                with self.subTest(field=field, invalid=invalid):
                    result = _ready_result()
                    result["analysis"]["coverage"][field] = invalid
                    with self.assertRaisesRegex(ValueError, "coverage"):
                        build_snapshot("closing", result, now=NOW, environ=ENVIRONMENT)

    def test_coverage_ready_and_unavailable_must_sum_to_total(self):
        for ready, total, unavailable in ((2, 1, 0), (1, 1, 1), (1, 2, 0)):
            with self.subTest(ready=ready, total=total, unavailable=unavailable):
                result = _ready_result()
                result["analysis"]["coverage"] = {
                    "ready": ready, "total": total, "unavailable": unavailable,
                }
                with self.assertRaisesRegex(ValueError, "coverage"):
                    build_snapshot("closing", result, now=NOW, environ=ENVIRONMENT)

    def test_validator_rejects_quality_coverage_disagreement(self):
        snapshot = build_snapshot("closing", _ready_result(), now=NOW, environ=ENVIRONMENT)
        snapshot["quality"]["analysis_ready"] = 2
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_snapshot(snapshot)

    def test_validator_rejects_ready_state_with_no_ready_coverage(self):
        snapshot = build_snapshot("closing", _ready_result(), now=NOW, environ=ENVIRONMENT)
        snapshot["result"]["analysis"]["coverage"] = {
            "ready": 0, "total": 1, "unavailable": 1,
        }
        snapshot["quality"].update(analysis_ready=0, analysis_unavailable=1)
        with self.assertRaisesRegex(ValueError, "analysis_state.*coverage"):
            validate_snapshot(snapshot)

    def test_validator_rejects_inner_outer_execution_status_disagreement(self):
        snapshot = build_snapshot("closing", _ready_result(), now=NOW, environ=ENVIRONMENT)
        snapshot["result"]["status"] = "partial"
        snapshot["result"]["errors"] = ["history evidence incomplete"]

        with self.assertRaisesRegex(ValueError, "result/status mismatch"):
            validate_snapshot(snapshot, environ=ENVIRONMENT)

    def test_validator_rejects_bar_summary_that_disagrees_with_raw_evidence(self):
        for raw_state in ("forming", "pending", None):
            with self.subTest(raw_state=raw_state):
                snapshot = build_snapshot("closing", _ready_result(), now=NOW, environ=ENVIRONMENT)
                snapshot["result"]["analysis"]["items"][0]["bar_status"] = raw_state
                with self.assertRaisesRegex(ValueError, "bar_status/analysis mismatch"):
                    validate_snapshot(snapshot, environ=ENVIRONMENT)

    def test_completed_snapshot_requires_item_count_to_match_coverage(self):
        for items in (None, [], [{"status": "ready", "bar_status": "complete"}]):
            with self.subTest(items=items):
                result = _ready_result()
                result["analysis"]["coverage"] = {"ready": 2, "total": 2, "unavailable": 0}
                result["analysis"]["items"] = items
                with self.assertRaisesRegex(ValueError, "coverage evidence"):
                    build_snapshot("closing", result, now=NOW, environ=ENVIRONMENT)

    def test_completed_snapshot_requires_item_statuses_to_match_coverage(self):
        for item in (None, {}, {"bar_status": "complete"}, {"status": "unavailable"}):
            with self.subTest(item=item):
                result = _ready_result()
                result["analysis"]["items"] = [item]
                with self.assertRaisesRegex(ValueError, "coverage evidence"):
                    build_snapshot("closing", result, now=NOW, environ=ENVIRONMENT)

    def test_partial_diagnostic_snapshot_does_not_require_complete_item_evidence(self):
        snapshot = build_snapshot("closing", _result(), now=NOW, environ=ENVIRONMENT)

        self.assertEqual(snapshot["status"], "partial")
        self.assertFalse(snapshot["quality"]["ready"])
        self.assertEqual(snapshot["bar_status"], {"daily": "UNKNOWN"})
        self.assertIs(validate_snapshot(snapshot, environ=ENVIRONMENT), snapshot)

    def test_analysis_state_rejects_not_ready_when_no_coverage(self):
        result = _result("trading")
        result["analysis"]["coverage"] = {"ready": 0, "total": 0, "unavailable": 0}
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(snapshot["analysis_state"], "NOT_READY")
        self.assertEqual(snapshot["quality"]["ready"], False)

    def test_failed_pipeline_maps_to_failed_state(self):
        result = _result("trading", status="failed")
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(snapshot["analysis_state"], "FAILED")
        self.assertEqual(snapshot["execution_status"], "failed")

    def test_bar_status_aggregates_mixed_states_to_forming(self):
        result = _result("trading")
        result["analysis"]["items"] = [
            {"bar_status": "complete"},
            {"bar_status": "completed_only"},
        ]
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(snapshot["bar_status"], {"daily": "FORMING"})

    def test_bar_status_aggregates_unavailable_to_missing(self):
        result = _result("trading")
        result["analysis"]["items"] = [
            {"bar_status": "complete"},
            {"bar_status": "unavailable"},
        ]
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(snapshot["bar_status"], {"daily": "MISSING"})

    def test_bar_status_aggregates_all_complete_to_closed(self):
        result = _result("trading")
        result["analysis"]["items"] = [
            {"bar_status": "complete"},
            {"bar_status": "complete"},
        ]
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(snapshot["bar_status"], {"daily": "CLOSED"})

    def test_validator_rejects_cutoff_market_date_mismatch(self):
        snapshot = build_snapshot("trading", _result("trading"), now=NOW, environ=ENVIRONMENT)
        snapshot["market_date"] = "2026-09-10"
        with self.assertRaisesRegex(ValueError, "data_cutoff/market_date mismatch"):
            validate_snapshot(snapshot)

    def test_validator_requires_canonical_report_cutoff_time(self):
        snapshot = build_snapshot("trading", _result("trading"), now=NOW, environ=ENVIRONMENT)
        snapshot["data_cutoff"] = "2026-09-11T14:29:00+08:00"
        with self.assertRaisesRegex(ValueError, "does not match report_kind cutoff"):
            validate_snapshot(snapshot)

    def test_validator_rejects_unknown_analysis_state(self):
        snapshot = build_snapshot("trading", _result("trading"), now=NOW, environ=ENVIRONMENT)
        snapshot["analysis_state"] = "MAYBE"
        with self.assertRaisesRegex(ValueError, "analysis_state"):
            validate_snapshot(snapshot)

    def test_validator_rejects_stale_rule_version(self):
        snapshot = build_snapshot("trading", _result("trading"), now=NOW, environ=ENVIRONMENT)
        snapshot["rule_version"] = "8.0"
        with self.assertRaisesRegex(ValueError, "rule_version"):
            validate_snapshot(snapshot)

    def test_manifest_hashes_current_and_each_available_stage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current = root / "current.json"
            current_snapshot = build_snapshot(
                "closing", _ready_result(), now=NOW, environ=ENVIRONMENT
            )
            current.write_text(
                json.dumps(current_snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (latest / "closing.json").write_bytes(current.read_bytes())
            output = root / "manifest.json"

            manifest = build_manifest(latest, current, output, now=NOW)

            expected_hash = hashlib.sha256(current.read_bytes()).hexdigest()
            raw = current.read_bytes()
            expected_blob_sha = hashlib.sha1(
                f"blob {len(raw)}\0".encode("ascii") + raw
            ).hexdigest()
            self.assertEqual(manifest["current"]["sha256"], expected_hash)
            self.assertEqual(manifest["current"]["git_blob_sha"], expected_blob_sha)
            self.assertEqual(manifest["latest"]["closing"]["sha256"], expected_hash)
            self.assertEqual(
                manifest["latest"]["closing"]["git_blob_sha"],
                expected_blob_sha,
            )
            self.assertEqual(manifest["latest"]["closing"]["run_id"], "12345")
            self.assertEqual(manifest["latest"]["closing"]["source_sha"], "abc123")
            self.assertTrue(manifest["publishable"])
            self.assertIsNone(manifest["data_not_ready_reason"])
            self.assertEqual(manifest["superseded"], [])
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), manifest)

    def test_manifest_rejects_a_stage_stored_under_the_wrong_name(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current = root / "current.json"
            snapshot = build_snapshot("closing", _result(), now=NOW, environ=ENVIRONMENT)
            content = json.dumps(snapshot, ensure_ascii=False)
            current.write_text(content, encoding="utf-8")
            (latest / "trading.json").write_text(content, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "filename/report_kind mismatch"):
                build_manifest(latest, current, root / "manifest.json", now=NOW)


class GitHubExportHardeningTests(unittest.TestCase):
    """P0 boundary regressions: fail closed on ambiguous or stale evidence."""

    def test_snapshot_before_cutoff_is_forced_not_ready(self):
        snapshot = build_snapshot(
            "trading",
            _result("trading"),
            now=EARLY_NOW,
            environ=ENVIRONMENT,
        )

        self.assertEqual(snapshot["analysis_state"], "NOT_READY")
        self.assertEqual(snapshot["execution_status"], "partial")
        self.assertEqual(snapshot["quality"]["ready"], False)
        self.assertTrue(
            any("GITHUB-EXPORT-001" in error for error in snapshot["quality"]["errors"])
        )
        validate_snapshot(snapshot)

    def test_snapshot_before_cutoff_with_no_coverage_stays_not_ready(self):
        result = _result("trading")
        result["analysis"]["coverage"] = {"ready": 0, "total": 0, "unavailable": 0}
        snapshot = build_snapshot(
            "trading", result, now=EARLY_NOW, environ=ENVIRONMENT
        )

        self.assertEqual(snapshot["analysis_state"], "NOT_READY")

    def test_snapshot_before_cutoff_keeps_failed_state(self):
        result = _result("trading", status="failed")
        snapshot = build_snapshot(
            "trading", result, now=EARLY_NOW, environ=ENVIRONMENT
        )

        self.assertEqual(snapshot["analysis_state"], "FAILED")
        self.assertEqual(snapshot["execution_status"], "failed")

    def test_validator_rejects_early_snapshot_claiming_analyzable_state(self):
        snapshot = build_snapshot("trading", _result("trading"), now=NOW, environ=ENVIRONMENT)
        snapshot["generated_at"] = "2026-09-11T10:00:00+08:00"
        snapshot["result"]["analysis"]["coverage"] = {"ready": 13, "total": 13, "unavailable": 0}

        with self.assertRaisesRegex(ValueError, "earlier than data_cutoff"):
            validate_snapshot(snapshot)

    def test_validator_rejects_empty_provenance(self):
        for field in ("repository", "workflow", "run_id", "source_sha"):
            with self.subTest(field=field):
                snapshot = build_snapshot(
                    "trading", _result("trading"), now=NOW, environ=ENVIRONMENT
                )
                snapshot["producer"][field] = "  "
                with self.assertRaisesRegex(ValueError, f"producer.{field}"):
                    validate_snapshot(snapshot)

    def test_build_snapshot_without_github_environment_fails_closed(self):
        with self.assertRaisesRegex(ValueError, r"producer\.(repository|run_id)"):
            build_snapshot("trading", _result("trading"), now=NOW, environ={})

    def test_bar_status_unknown_value_is_not_closed(self):
        result = _result("trading")
        result["analysis"]["items"] = [
            {"status": "ready", "bar_status": "complete"},
            {"status": "ready", "bar_status": "pending"},
        ]
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(snapshot["bar_status"], {"daily": "UNKNOWN"})

    def test_bar_status_missing_on_ready_holding_is_not_closed(self):
        result = _result("trading")
        result["analysis"]["items"] = [
            {"status": "ready", "bar_status": "complete"},
            {"status": "ready"},
        ]
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(snapshot["bar_status"], {"daily": "MISSING"})

    def test_bar_status_unavailable_holding_is_not_ignored(self):
        result = _result("trading")
        result["analysis"]["items"] = [
            {"status": "ready", "bar_status": "complete"},
            {"status": "unavailable", "reason": "历史K线获取失败"},
        ]
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(snapshot["bar_status"], {"daily": "MISSING"})

    def test_bar_status_without_items_is_unknown(self):
        result = _result("trading")
        result["analysis"]["items"] = []
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(snapshot["bar_status"], {"daily": "UNKNOWN"})

    def test_bar_status_merges_multi_cycle_states_without_dropping_them(self):
        result = _result("trading")
        result["analysis"]["items"] = [
            {"status": "ready", "bar_status": {"daily": "complete", "120m": "completed_only"}},
            {"status": "ready", "bar_status": {"daily": "complete", "120m": "unavailable"}},
        ]
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(
            snapshot["bar_status"], {"120m": "MISSING", "daily": "CLOSED"}
        )

    def test_bar_status_contract_states_are_aggregated_by_worst_case(self):
        result = _result("trading")
        result["analysis"]["items"] = [
            {"status": "ready", "bar_status": {"daily": "CLOSED", "30m": "FORMING"}},
            {"status": "ready", "bar_status": {"daily": "FORMING", "30m": "CLOSED"}},
        ]
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(
            snapshot["bar_status"], {"30m": "FORMING", "daily": "FORMING"}
        )

    def test_bar_status_partial_cycle_coverage_downgrades_unreported_cycles(self):
        result = _result("trading")
        result["analysis"]["items"] = [
            {"status": "ready", "bar_status": {"daily": "complete"}},
            {"status": "ready", "bar_status": {"daily": "complete", "15m": "complete"}},
        ]
        snapshot = build_snapshot(
            "trading", result, now=NOW, environ=ENVIRONMENT
        )
        self.assertEqual(
            snapshot["bar_status"], {"15m": "MISSING", "daily": "CLOSED"}
        )

    def test_validator_rejects_unknown_bar_status_state(self):
        snapshot = build_snapshot("trading", _result("trading"), now=NOW, environ=ENVIRONMENT)
        snapshot["bar_status"] = {"daily": "PROBABLY"}
        with self.assertRaisesRegex(ValueError, "bar_status"):
            validate_snapshot(snapshot)

    def test_validator_rejects_missing_bar_status(self):
        snapshot = build_snapshot("trading", _result("trading"), now=NOW, environ=ENVIRONMENT)
        del snapshot["bar_status"]
        with self.assertRaisesRegex(ValueError, "bar_status"):
            validate_snapshot(snapshot)

    def test_validator_rejects_status_execution_status_mismatch(self):
        snapshot = build_snapshot("trading", _result("trading"), now=NOW, environ=ENVIRONMENT)
        snapshot["execution_status"] = "completed"
        snapshot["status"] = "partial"
        with self.assertRaisesRegex(ValueError, "status/execution_status mismatch"):
            validate_snapshot(snapshot)

    def test_validator_rejects_failed_execution_claiming_analyzable_state(self):
        result = _result("trading")
        result["analysis"]["coverage"] = {"ready": 13, "total": 13, "unavailable": 0}
        snapshot = build_snapshot("trading", result, now=NOW, environ=ENVIRONMENT)
        snapshot["analysis_state"] = "READY"
        snapshot["execution_status"] = "failed"
        snapshot["status"] = "failed"
        with self.assertRaisesRegex(ValueError, "not consistent with execution_status"):
            validate_snapshot(snapshot)

    def test_manifest_rejects_current_latest_content_mismatch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current_snapshot = build_snapshot(
                "closing", _result(), now=NOW, environ=ENVIRONMENT
            )
            (root / "current.json").write_text(
                json.dumps(current_snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            other = build_snapshot(
                "closing", _result(), now=datetime(2026, 9, 12, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai")), environ=ENVIRONMENT
            )
            (latest / "closing.json").write_text(
                json.dumps(other, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "current/latest content mismatch"):
                build_manifest(latest, root / "current.json", root / "manifest.json", now=NOW)

    def test_manifest_records_legacy_latest_snapshot_without_deleting_it(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current_snapshot = build_snapshot(
                "closing", _result(), now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (latest / "closing.json").write_bytes(current_path.read_bytes())
            # A pre-V2 snapshot on the persistent data branch (schema_version 1).
            legacy = {
                "schema_version": 1,
                "report_kind": "trading",
                "generated_at": "2026-09-10T14:35:00+08:00",
                "status": "completed",
            }
            (latest / "trading.json").write_text(
                json.dumps(legacy, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            manifest = build_manifest(latest, current_path, root / "manifest.json", now=NOW)

            self.assertTrue((latest / "trading.json").exists())
            self.assertEqual(list(manifest["latest"]), ["closing"])
            self.assertEqual(len(manifest["superseded"]), 1)
            self.assertEqual(
                manifest["superseded"][0]["path"],
                "data/chatgpt/latest/trading.json",
            )
            self.assertIn("legacy schema_version 1 superseded", manifest["superseded"][0]["reason"])

    def test_manifest_rejects_invalid_v2_latest_and_preserves_the_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current_snapshot = build_snapshot(
                "closing", _result(), now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (latest / "closing.json").write_bytes(current_path.read_bytes())
            corrupt = latest / "trading.json"
            corrupt.write_text(
                json.dumps({"schema_version": 2, "tampered": True}) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "file was preserved"):
                build_manifest(latest, current_path, root / "manifest.json", now=NOW)
            self.assertTrue(corrupt.exists())

    def test_manifest_requires_latest_entry_for_current_kind(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current_snapshot = build_snapshot(
                "closing", _result(), now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "missing the latest entry"):
                build_manifest(latest, current_path, root / "manifest.json", now=NOW)

    def test_manifest_marks_not_ready_current_as_not_publishable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            result = _result("trading")
            result["analysis"]["coverage"] = {"ready": 0, "total": 13, "unavailable": 13}
            current_snapshot = build_snapshot(
                "trading", result, now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (latest / "trading.json").write_bytes(current_path.read_bytes())

            manifest = build_manifest(latest, current_path, root / "manifest.json", now=NOW)

            self.assertFalse(manifest["publishable"])
            self.assertIn("NOT_READY", manifest["data_not_ready_reason"])

    def test_manifest_marks_early_gated_snapshot_as_not_publishable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current_snapshot = build_snapshot(
                "trading",
                _result("trading"),
                now=EARLY_NOW,
                environ=ENVIRONMENT,
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (latest / "trading.json").write_bytes(current_path.read_bytes())

            manifest = build_manifest(latest, current_path, root / "manifest.json", now=NOW)

            self.assertFalse(manifest["publishable"])
            self.assertIn("NOT_READY", manifest["data_not_ready_reason"])

    def test_manifest_marks_partial_current_as_not_publishable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current_snapshot = build_snapshot(
                "trading",
                _result("trading", status="partial"),
                now=NOW,
                environ=ENVIRONMENT,
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (latest / "trading.json").write_bytes(current_path.read_bytes())

            manifest = build_manifest(latest, current_path, root / "manifest.json", now=NOW)

            self.assertFalse(manifest["publishable"])
            self.assertIn("partial", manifest["data_not_ready_reason"])

    def test_manifest_marks_ready_current_as_publishable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            result = _ready_result("trading")
            current_snapshot = build_snapshot(
                "trading", result, now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (latest / "trading.json").write_bytes(current_path.read_bytes())

            manifest = build_manifest(latest, current_path, root / "manifest.json", now=NOW)

            self.assertTrue(manifest["publishable"])
            self.assertIsNone(manifest["data_not_ready_reason"])

    def test_manifest_rejects_stale_current_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current_snapshot = build_snapshot(
                "trading", _ready_result("trading"), now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (latest / "trading.json").write_bytes(current_path.read_bytes())

            manifest = build_manifest(
                latest,
                current_path,
                root / "manifest.json",
                now=datetime(
                    2026, 9, 11, 17, 1, tzinfo=ZoneInfo("Asia/Shanghai")
                ),
            )

            self.assertFalse(manifest["publishable"])
            self.assertIn("stale", manifest["data_not_ready_reason"])

    def test_manifest_accepts_snapshot_at_exact_freshness_boundary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current_snapshot = build_snapshot(
                "trading", _ready_result("trading"), now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (latest / "trading.json").write_bytes(current_path.read_bytes())

            manifest = build_manifest(
                latest,
                current_path,
                root / "manifest.json",
                now=NOW + timedelta(minutes=30),
            )

            self.assertTrue(manifest["publishable"])

    def test_manifest_rejects_naive_build_time(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current_snapshot = build_snapshot(
                "trading", _ready_result("trading"), now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (latest / "trading.json").write_bytes(current_path.read_bytes())

            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                build_manifest(
                    latest,
                    current_path,
                    root / "manifest.json",
                    now=datetime(2026, 9, 11, 16, 30),
                )

    def test_manifest_rejects_snapshot_generated_in_the_future(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current_snapshot = build_snapshot(
                "trading", _ready_result("trading"), now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (latest / "trading.json").write_bytes(current_path.read_bytes())

            manifest = build_manifest(
                latest,
                current_path,
                root / "manifest.json",
                now=datetime(
                    2026, 9, 11, 16, 29, 59,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            )

            self.assertFalse(manifest["publishable"])
            self.assertIn("future", manifest["data_not_ready_reason"])

    def test_closing_manifest_requires_all_reported_bars_to_be_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            result = _ready_result("closing")
            result["analysis"]["items"][0]["bar_status"] = "forming"
            current_snapshot = build_snapshot(
                "closing", result, now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (latest / "closing.json").write_bytes(current_path.read_bytes())

            manifest = build_manifest(
                latest, current_path, root / "manifest.json", now=NOW
            )

            self.assertFalse(manifest["publishable"])
            self.assertIn("FORMING", manifest["data_not_ready_reason"])

    def test_intraday_manifest_allows_known_forming_bar_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            result = _ready_result("trading")
            result["analysis"]["items"][0]["bar_status"] = "forming"
            current_snapshot = build_snapshot(
                "trading", result, now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (latest / "trading.json").write_bytes(current_path.read_bytes())

            manifest = build_manifest(
                latest, current_path, root / "manifest.json", now=NOW
            )

            self.assertTrue(manifest["publishable"])

    def test_manifest_rejects_missing_or_unknown_bar_evidence(self):
        for bar_state in ("MISSING", "UNKNOWN"):
            with self.subTest(bar_state=bar_state), TemporaryDirectory() as directory:
                root = Path(directory)
                latest = root / "latest"
                latest.mkdir()
                result = _ready_result("trading")
                result["analysis"]["items"][0]["bar_status"] = bar_state
                current_snapshot = build_snapshot(
                    "trading", result, now=NOW, environ=ENVIRONMENT
                )
                current_path = root / "current.json"
                current_path.write_text(
                    json.dumps(current_snapshot, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                (latest / "trading.json").write_bytes(current_path.read_bytes())

                manifest = build_manifest(
                    latest, current_path, root / "manifest.json", now=NOW
                )

                self.assertFalse(manifest["publishable"])
                self.assertIn("bar_status", manifest["data_not_ready_reason"])

    def test_gate_command_fails_on_not_publishable_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            current_snapshot = build_snapshot(
                "trading",
                _result("trading"),
                now=EARLY_NOW,
                environ=ENVIRONMENT,
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (latest / "trading.json").write_bytes(current_path.read_bytes())
            manifest_path = root / "manifest.json"
            build_manifest(latest, current_path, manifest_path, now=NOW)

            exit_code = main(["gate", "--manifest", str(manifest_path)])

            self.assertEqual(exit_code, 4)

    def test_gate_command_passes_on_publishable_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            result = _ready_result("trading")
            current_snapshot = build_snapshot(
                "trading", result, now=NOW, environ=ENVIRONMENT
            )
            current_path = root / "current.json"
            current_path.write_text(
                json.dumps(current_snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (latest / "trading.json").write_bytes(current_path.read_bytes())
            manifest_path = root / "manifest.json"
            build_manifest(latest, current_path, manifest_path, now=NOW)

            exit_code = main(["gate", "--manifest", str(manifest_path)])

            self.assertEqual(exit_code, 0)

    def test_gate_command_fails_on_manifest_without_publishable_flag(self):
        with TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")

            exit_code = main(["gate", "--manifest", str(manifest_path)])

            self.assertEqual(exit_code, 4)


class PublishWorkflowHardeningTests(unittest.TestCase):
    """The publish workflow must fail CI on non-publishable data releases."""

    WORKFLOW = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "publish-chatgpt-data.yml"
    )

    @classmethod
    def setUpClass(cls):
        cls.doc = yaml.safe_load(cls.WORKFLOW.read_text(encoding="utf-8"))

    def _step(self, name):
        steps = self.doc["jobs"]["publish"]["steps"]
        matches = [step for step in steps if step.get("name") == name]
        self.assertEqual(len(matches), 1, f"step {name!r} must exist exactly once")
        return matches[0]

    def test_publish_step_gates_the_manifest(self):
        step = self._step("Publish current and latest snapshots")
        script = step["run"]
        self.assertIn("github_export gate", script)
        self.assertIn("gate_exit=${gate_exit}", script)
        # The gate must not abort before the diagnostic data is committed.
        self.assertIn("set +e", script)

    def test_final_gate_step_fails_the_job_on_non_publishable_data(self):
        step = self._step("Gate publishability")
        self.assertEqual(step.get("if"), "always()")
        environment = step.get("env", {})
        self.assertEqual(
            environment.get("GATE_EXIT"), "${{ steps.publish.outputs.gate_exit }}"
        )
        self.assertEqual(
            environment.get("COLLECT_EXIT"), "${{ steps.collect.outputs.collect_exit }}"
        )
        self.assertIn("exit 1", step["run"])

    def test_workflow_no_longer_masks_degraded_collection_as_success(self):
        content = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("Record degraded collection", content)
        self.assertNotIn(
            "failure snapshot was still published", content
        )
        self.assertIn("git add -A data/chatgpt", content)

    def test_scheduled_collection_starts_after_each_data_cutoff(self):
        content = self.WORKFLOW.read_text(encoding="utf-8")
        for cron in (
            '31 22 * * 0-4',
            '1 1 * * 1-5',
            '31 3 * * 1-5',
            '31 6 * * 1-5',
            '11 8 * * 1-5',
        ):
            with self.subTest(cron=cron):
                self.assertIn(f'cron: "{cron}"', content)


if __name__ == "__main__":
    unittest.main()
