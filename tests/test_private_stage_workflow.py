"""Synthetic end-to-end observations, not live-account acceptance evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.workflow.portfolio import run_portfolio_report
from tests.test_portfolio_workflow import _config, _Router, _Notifier, _valuation
from tests.test_stage_evidence import MORNING, MIDDAY


def run_stage(kind, at, directory=None, price="10", **kwargs):
    return run_portfolio_report(
        kind, config_loader=_config,
        valuation_router=_Router({"600000": replace(_valuation(), as_of=at, price=Decimal(price))}),
        strategy_loader=lambda: {}, analysis_enabled=False,
        clock=lambda: at, private_state_dir=directory, **kwargs,
    )


class PrivateStageWorkflowTests(unittest.TestCase):
    def test_explicit_run_identity_is_preserved_without_generating_another(self):
        with patch("app.workflow.portfolio.uuid4") as generate:
            result = run_stage("morning", MORNING, run_id="synthetic-reserved-run")
        generate.assert_not_called()
        self.assertEqual(result["run_id"], "synthetic-reserved-run")
        self.assertEqual(result["status"], "completed")

    def test_invalid_run_identity_fails_before_account_access_or_notification(self):
        for run_id in (False, [], "", " ", "x" * 1001):
            with self.subTest(run_id_type=type(run_id).__name__):
                loader, notifier = Mock(), Mock()
                result = run_portfolio_report(
                    "morning", config_loader=loader, notifier=notifier,
                    clock=lambda: MORNING, run_id=run_id,
                )
                self.assertEqual(result["status"], "failed")
                loader.assert_not_called()
                notifier.send.assert_not_called()

    def test_invalid_clocks_fail_before_account_access_or_notification(self):
        for value in (None, False, "fixture-private-clock", datetime(2026, 9, 15, 9),
                      datetime.max.replace(tzinfo=timezone.utc)):
            with self.subTest(value=type(value).__name__):
                loader, notifier = Mock(), Mock()
                result = run_portfolio_report("morning", config_loader=loader, notifier=notifier, clock=lambda: value)
                self.assertEqual(result["status"], "failed")
                loader.assert_not_called()
                notifier.send.assert_not_called()
                self.assertNotIn("fixture-private-clock", str(result))

    def test_clock_exception_and_false_clock_are_not_silently_replaced(self):
        for clock in (False, Mock(side_effect=RuntimeError("fixture-private-clock"))):
            loader = Mock()
            result = run_portfolio_report("morning", config_loader=loader, clock=clock)
            self.assertEqual(result["status"], "failed")
            loader.assert_not_called()
            self.assertNotIn("fixture-private-clock", str(result))

    def test_explicit_false_now_is_rejected_instead_of_using_live_time(self):
        loader = Mock()
        result = run_portfolio_report("morning", config_loader=loader, now=False, clock=lambda: MORNING)
        self.assertEqual(result["status"], "failed")
        loader.assert_not_called()

    def test_backward_clock_stops_delivery_and_stage_writes(self):
        for samples in ((MORNING, MORNING - timedelta(seconds=1)),
                        (MORNING, MORNING, MORNING - timedelta(seconds=1))):
            with self.subTest(samples=samples), tempfile.TemporaryDirectory() as raw:
                notifier = Mock()
                result = run_portfolio_report(
                    "morning", config_loader=_config, valuation_router=_Router({"600000": _valuation()}),
                    strategy_loader=lambda: {}, analysis_enabled=False, notifier=notifier,
                    private_state_dir=raw, clock=Mock(side_effect=samples),
                )
                self.assertEqual(result["status"], "failed")
                notifier.send.assert_not_called()
                self.assertEqual(list(Path(raw).iterdir()), [])

    def test_raw_exception_messages_do_not_leak_into_structured_results(self):
        result = run_portfolio_report("morning",
                                      config_loader=Mock(side_effect=ValueError("fixture-private-account-detail")),
                                      clock=lambda: MORNING)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"], ["workflow: ValueError"])
        self.assertNotIn("fixture-private-account-detail", str(result))

    def test_unhashable_report_kind_fails_before_account_access(self):
        loader = Mock()
        result = run_portfolio_report([], config_loader=loader, clock=lambda: MORNING)
        self.assertEqual(result["status"], "failed")
        loader.assert_not_called()
        self.assertIn("WAIT", result["report"])

    def test_pipeline_success_does_not_claim_complete_analysis_or_chatgpt_delivery(self):
        result = run_stage("morning", MORNING, notifier=_Notifier())
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["full_analysis_ready"])
        self.assertEqual(result["analysis_state"], "NOT_READY")
        self.assertTrue(result["delivery"]["api_accepted"])
        self.assertFalse(result["delivery"]["chatgpt_received"])
        self.assertFalse(result["delivery"]["device_received"])
        self.assertIsNone(result["delivery"]["api_receipt_id"])
        self.assertEqual(result["stage_context"]["persistence"]["status"], "not_configured")

    def test_same_pipeline_persists_morning_and_uses_it_for_midday(self):
        with tempfile.TemporaryDirectory() as raw:
            morning = run_stage("morning", MORNING, raw)
            midday = run_stage("midday", MIDDAY, raw, price="12")
        self.assertEqual(morning["stage_context"]["persistence"]["status"], "stored")
        self.assertEqual(midday["stage_context"]["comparison"]["status"], "comparable")
        self.assertIn("参考价 10 → 12", midday["report"])
        self.assertIn("市值变化 200.00", midday["report"])
        self.assertNotIn("贡献最强", midday["report"])
        self.assertNotEqual(morning["run_id"], midday["run_id"])

    def test_public_execution_is_rejected_before_real_config_is_read(self):
        config_loader = Mock(side_effect=AssertionError("must not read account"))
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY_VISIBILITY": "public"}, clear=True):
            result = run_portfolio_report("morning", config_loader=config_loader, clock=lambda: MORNING)
        config_loader.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["full_analysis_ready"])

    def test_unsafe_state_directory_rejected_before_account_access(self):
        loader = Mock(side_effect=AssertionError("must not read account"))
        result = run_portfolio_report("morning", config_loader=loader,
                                      private_state_dir=Path.cwd() / "data", clock=lambda: MORNING)
        loader.assert_not_called()
        self.assertEqual(result["status"], "failed")

    def test_offline_now_override_cannot_write_live_stage_records(self):
        with tempfile.TemporaryDirectory() as raw:
            result = run_stage("morning", MIDDAY, raw, now=MORNING)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(list(Path(raw).iterdir()), [])

    def test_rule_config_change_disables_baseline_comparison(self):
        with tempfile.TemporaryDirectory() as raw:
            run_stage("morning", MORNING, raw)
            result = run_portfolio_report(
                "midday", config_loader=_config,
                valuation_router=_Router({"600000": replace(_valuation(), as_of=MIDDAY)}),
                strategy_loader=lambda: {"test_rule": "changed"}, analysis_enabled=False,
                clock=lambda: MIDDAY, private_state_dir=raw,
            )
        self.assertEqual(result["stage_context"]["comparison"]["status"], "uncomparable")
        self.assertIn("rule_version_changed", result["stage_context"]["comparison"]["reasons"])


if __name__ == "__main__":
    unittest.main()
