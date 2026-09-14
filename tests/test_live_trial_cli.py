"""Offline private lifecycle commands, using synthetic calendar/receipt data."""

from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.integration import live_trial as cli
from app.workflow.live_acceptance import LiveTrialLedger
from app.workflow.portfolio import portfolio_rule_version
from tests.test_live_acceptance import TRADING_DATES, aware, passing_event


NOW = aware("2026-09-15", "08:59")


class LiveTrialCliTests(unittest.TestCase):
    def manifest(self, raw, **overrides):
        path = Path(raw) / "calendar.json"
        calendar = {"schema": "private_trial_calendar.v1", "source": "synthetic-calendar",
                    "ordered_trading_dates": list(TRADING_DATES),
                    "calendar_coverage": list(TRADING_DATES), "verified": False}
        calendar.update(overrides)
        path.write_text(json.dumps(calendar), encoding="utf-8")
        return path

    def run_cli(self, args):
        output = StringIO()
        with redirect_stdout(output):
            code = cli.main(args)
        return code, json.loads(output.getvalue())

    def init(self, raw, **overrides):
        calendar = self.manifest(raw, **overrides)
        path = Path(raw) / "trial.jsonl"
        with patch("app.workflow.live_acceptance._recording_time", return_value=NOW):
            result = self.run_cli(["init", "--ledger", str(path), "--calendar", str(calendar),
                                   "--start", "2026-09-15"])
        return path, result

    def test_initialization_freezes_current_rules_without_starting_or_claiming_success(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(cli, "portfolio_rule_version", return_value="rules-fixture"):
            path, (code, result) = self.init(raw)
            ledger = LiveTrialLedger.load(path)
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "initialized_unverified")
            self.assertEqual(ledger.rule_version, "rules-fixture")
            self.assertFalse(ledger.calendar_verified)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)
            self.assertFalse(result["scheduler_started"])
            self.assertFalse(result["auto_execute"])

    def test_calendar_true_remains_a_caller_claim_not_external_verification(self):
        with tempfile.TemporaryDirectory() as raw:
            path, (_, result) = self.init(raw, verified=True)
            self.assertFalse(result["independently_verified"])
            code, status = self.run_cli(["status", "--ledger", str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(status["calendar_verification"], "caller_claim_only")
            self.assertFalse(status["independently_verified"])
            self.assertNotEqual(status["status"], "passed")

    def test_initialize_never_overwrites_existing_ledger(self):
        with tempfile.TemporaryDirectory() as raw:
            path, _ = self.init(raw)
            before = path.read_bytes()
            _, (code, result) = self.init(raw)
            self.assertEqual(code, 1)
            self.assertEqual(result["error_type"], "FileExistsError")
            self.assertEqual(path.read_bytes(), before)

    def test_duplicate_json_keys_and_nonboolean_verification_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            for verification in ("true", 1, None):
                with self.subTest(verification=verification):
                    path, (code, _) = self.init(raw, verified=verification)
                    self.assertEqual(code, 1)
                    self.assertFalse(path.exists())
            calendar = self.manifest(raw)
            calendar.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
            code, _ = self.run_cli(["init", "--ledger", str(Path(raw) / "trial.jsonl"),
                                    "--calendar", str(calendar), "--start", "2026-09-15"])
            self.assertEqual(code, 1)

    def test_public_execution_stops_before_calendar_or_rule_read(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY_VISIBILITY": "public"}, clear=True), patch.object(
            cli, "read_private_bytes"
        ) as read, patch.object(cli, "portfolio_rule_version") as rules:
            code, _ = self.run_cli(["init", "--ledger", ".private/trial.jsonl",
                                   "--calendar", ".private/calendar.json", "--start", "2026-09-15"])
            self.assertEqual(code, 1)
            read.assert_not_called()
            rules.assert_not_called()

    def test_receipt_records_only_an_existing_run_and_is_never_independently_verified(self):
        with tempfile.TemporaryDirectory() as raw:
            path, _ = self.init(raw)
            ledger = LiveTrialLedger.load(path)
            at = NOW + timedelta(minutes=1)
            event = passing_event("2026-09-15", "09:00", rule_version=ledger.rule_version,
                calendar_source=ledger.calendar_source, chatgpt_received=False, device_received=False,
                chatgpt_ack_id=None, device_receipt_id=None)
            ledger.append(event, clock=lambda: at)
            receipt = Path(raw) / "receipt.json"
            receipt.write_text(json.dumps({"run_id": event.run_id, "trading_date": event.trading_date,
                "stage": event.stage, "chatgpt_ack_id": "synthetic-ack", "device_receipt_id": None,
                "receipt_observed_at": at.isoformat()}), encoding="utf-8")
            with patch("app.workflow.live_acceptance._recording_time", return_value=at):
                code, result = self.run_cli(["record-receipt", "--ledger", str(path),
                                            "--receipt-file", str(receipt)])
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "recorded_unverified")
            self.assertFalse(result["independently_verified"])
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)

    def test_failure_messages_never_echo_private_payload_or_exception(self):
        with patch.object(cli, "read_private_bytes", side_effect=ValueError("private-fixture-value")):
            code, result = self.run_cli(["init", "--ledger", ".private/trial.jsonl",
                "--calendar", ".private/calendar.json", "--start", "2026-09-15"])
        self.assertEqual(code, 1)
        self.assertNotIn("private-fixture-value", str(result))

    def test_rule_fingerprint_changes_when_analysis_is_disabled(self):
        self.assertNotEqual(portfolio_rule_version({}, analysis_enabled=True),
                            portfolio_rule_version({}, analysis_enabled=False))


if __name__ == "__main__":
    unittest.main()
