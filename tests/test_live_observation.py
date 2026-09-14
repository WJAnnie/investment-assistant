"""Synthetic observation wiring; never evidence of a real account trial."""

from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.workflow.live_acceptance import LiveTrialLedger
from app.workflow.live_observation import observe_report
from app.workflow.portfolio import run_portfolio_report
from app.workflow.stage_store import StageStore
from app.workflow import scheduler as scheduling
from tests.test_live_acceptance import TRADING_DATES, aware
from tests.test_private_stage_workflow import run_stage
from tests.test_portfolio_workflow import _config, _Router, _valuation
from tests.test_scheduler import _Scheduler


NOW = aware("2026-09-15", "09:00")


def make_ledger(directory, rule="synthetic-rule"):
    return LiveTrialLedger.create(
        Path(directory) / "trial.jsonl", trial_start="2026-09-15",
        rule_version=rule, calendar_source="synthetic-calendar",
        ordered_trading_dates=TRADING_DATES, calendar_coverage=TRADING_DATES,
        calendar_verified=True, clock=lambda: NOW - timedelta(minutes=1),
    )


def recorded_event(ledger):
    return json.loads(ledger.path.read_text(encoding="utf-8").splitlines()[-1])["event"]


class LiveObservationTests(unittest.TestCase):
    def test_reloads_persisted_stage_and_keeps_missing_receipts_and_reviews_false(self):
        with tempfile.TemporaryDirectory() as raw:
            stages = Path(raw) / "stages"
            result = run_stage("morning", NOW, stages)
            ledger = make_ledger(raw, result["rule_version"])
            result.update(notified=True, notification_attempted=True)
            result["delivery"].update(api_accepted=True, chatgpt_received=True, device_received=True,
                                      api_receipt_id="untrusted-api-id", chatgpt_ack_id="untrusted-ack")
            observe_report(ledger, result, private_state_dir=stages,
                           scheduled_at=NOW, observed_at=NOW, clock=lambda: NOW)
            event = recorded_event(ledger)
            self.assertEqual(event["run_id"], result["run_id"])
            self.assertEqual(event["data_evidence_id"], result["stage_context"]["evidence"]["evidence_fingerprint"])
            self.assertTrue(event["api_accepted"])
            self.assertTrue(event["report_valid"])
            self.assertFalse(event["data_complete"])
            for field in ("chatgpt_received", "device_received", "strategy_success",
                          "user_review_second_buy_ok", "no_overtrading_ok", "position_increment_validated"):
                self.assertIs(event[field], False)
            for field in ("api_receipt_id", "chatgpt_ack_id", "device_receipt_id",
                          "strategy_evidence_id", "review_evidence_id", "position_evidence_id"):
                self.assertIsNone(event[field])
            self.assertEqual(result["live_acceptance_status"], "recorded_unverified")
            self.assertFalse(ledger.summary(as_of=NOW)["independently_verified"])

    def test_claimed_full_analysis_cannot_override_missing_source_observations(self):
        with tempfile.TemporaryDirectory() as raw:
            stages = Path(raw) / "stages"
            result = run_stage("morning", NOW, stages)
            result.update(full_analysis_ready=True, analysis_state="READY", analysis_gaps=[])
            ledger = make_ledger(raw, result["rule_version"])
            observe_report(ledger, result, private_state_dir=stages,
                           scheduled_at=NOW, observed_at=NOW, clock=lambda: NOW)
            self.assertFalse(recorded_event(ledger)["data_complete"])

    def test_missing_corrupt_or_different_first_stage_is_recorded_as_invalid(self):
        for scenario in ("missing", "corrupt", "different", "claimed-persistence"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as raw:
                stages = Path(raw) / "stages"
                result = run_stage("morning", NOW, None if scenario == "missing" else stages)
                ledger = make_ledger(raw, result["rule_version"])
                if scenario == "corrupt":
                    # Deliberate synthetic corruption, not a private data fixture.
                    (stages / "2026-09-15" / "morning.json").write_text("{}", encoding="utf-8")
                elif scenario == "different":
                    result = run_stage("morning", NOW, stages, price="12")
                elif scenario == "claimed-persistence":
                    result["stage_context"]["persistence"]["status"] = "failed"
                observe_report(ledger, result, private_state_dir=stages,
                               scheduled_at=NOW, observed_at=NOW, clock=lambda: NOW)
                event = recorded_event(ledger)
                self.assertFalse(event["report_valid"])
                self.assertFalse(event["data_complete"])
                self.assertIsNone(event["data_evidence_id"])

    def test_late_failed_observation_keeps_actual_time_and_does_not_backdate(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            result = {"run_id": "synthetic-failure", "report_kind": "morning",
                      "status": "failed", "report": "WAIT", "errors": []}
            late = NOW + timedelta(minutes=11)
            observe_report(ledger, result, private_state_dir=Path(raw) / "stages",
                           scheduled_at=NOW, observed_at=late, clock=lambda: late)
            event = recorded_event(ledger)
            self.assertEqual(event["ingested_at"], late.isoformat())
            self.assertEqual(event["latency_seconds"], 660)
            summary = ledger.summary(as_of=late)
            self.assertEqual(summary["days"]["2026-09-15"]["slots"]["09:00"]["status"], "failed")

    def test_ledger_write_failure_is_visible_without_sensitive_exception_text(self):
        with tempfile.TemporaryDirectory() as raw:
            result = run_stage("morning", NOW, Path(raw) / "stages")
            ledger = make_ledger(raw, result["rule_version"])
            with patch.object(ledger, "append", side_effect=OSError("private-account-fixture")):
                observe_report(ledger, result, private_state_dir=Path(raw) / "stages",
                               scheduled_at=NOW, observed_at=NOW, clock=lambda: NOW)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["live_acceptance_status"], "record_failed")
            self.assertNotIn("private-account-fixture", str(result))


class LiveObservationSchedulerTests(unittest.TestCase):
    def test_workflow_identity_conflict_is_not_relabelled_or_sent(self):
        for wrong_field in ("run_id", "report_kind"):
            with self.subTest(wrong_field=wrong_field), tempfile.TemporaryDirectory() as raw:
                stages = Path(raw) / "stages"
                ledger = make_ledger(raw, run_stage("morning", NOW)["rule_version"])
                notifier = Mock(send=Mock(return_value=True))
                def collect(**options):
                    report = run_stage("morning", NOW, stages)
                    report["run_id"] = options.get("run_id")
                    report[wrong_field] = "synthetic-conflicting-identity"
                    return report
                with patch.object(scheduling, "run_portfolio_report", side_effect=collect) as collect_mock:
                    result = scheduling._run_four_stage_report(
                        notifier, "morning", "morning", private_state_dir=stages,
                        trial_ledger=ledger, clock=lambda: NOW)
                notifier.send.assert_not_called()
                self.assertFalse(result["notification_attempted"])
                self.assertFalse(recorded_event(ledger)["report_valid"])
                self.assertEqual(collect_mock.call_args.kwargs["run_id"], result["run_id"])

    def test_caught_clock_failure_is_not_cleared_before_sending_a_safe_wait(self):
        for use_ledger in (False, True):
            for failure_point in ("config", "store"):
                with self.subTest(ledger=use_ledger, failure=failure_point), tempfile.TemporaryDirectory() as raw:
                    stages = Path(raw) / "stages"
                    ledger = make_ledger(raw) if use_ledger else None
                    notifier = Mock(send=Mock(return_value=True))
                    prefix = [NOW, NOW] if use_ledger else [NOW]
                    if failure_point == "config":
                        samples = prefix + [NOW + timedelta(minutes=3), NOW + timedelta(minutes=2)]
                        loader = Mock(side_effect=RuntimeError("synthetic-config-failure"))
                    else:
                        samples = prefix + [NOW, NOW + timedelta(minutes=1),
                                            NOW + timedelta(minutes=3), NOW + timedelta(minutes=2)]
                        loader = _config
                    remaining = iter(samples)
                    def collect(**options):
                        return run_portfolio_report(
                            **options, config_loader=loader, valuation_router=_Router({"600000": _valuation()}),
                            strategy_loader=lambda: {}, analysis_enabled=False,
                        )
                    with patch.object(scheduling, "run_portfolio_report", side_effect=collect):
                        result = scheduling._run_four_stage_report(
                            notifier, "morning", "morning", private_state_dir=stages, trial_ledger=ledger,
                            clock=lambda: next(remaining, NOW + timedelta(minutes=4)))
                    notifier.send.assert_not_called()
                    self.assertFalse(result["notification_allowed"])
                    self.assertFalse(result["notification_attempted"])
                    if ledger is not None:
                        self.assertTrue(ledger.has_attempt("2026-09-15", "09:00"))

    def test_register_loads_existing_frozen_ledger_without_starting(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            target, notifier = _Scheduler(), Mock()
            scheduling.register_four_stage_jobs(target, notifier=notifier,
                private_state_dir=Path(raw) / "stages", live_ledger=ledger.path)
            self.assertEqual(len(target.jobs), 4)
            self.assertTrue(all(job[2]["kwargs"]["trial_ledger"].header == ledger.header for job in target.jobs))
            self.assertEqual(len(ledger.path.read_text(encoding="utf-8").splitlines()), 1)
            notifier.send.assert_not_called()

    def test_callback_records_api_outcome_after_one_send_attempt(self):
        with tempfile.TemporaryDirectory() as raw:
            stages = Path(raw) / "stages"
            result = run_stage("morning", NOW)
            ledger = make_ledger(raw, result["rule_version"])
            notifier = Mock()
            def send(_message):
                records = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual([record["record_type"] for record in records], ["header", "attempt"])
                return True
            notifier.send.side_effect = send
            with patch.object(scheduling, "run_portfolio_report",
                              side_effect=lambda **options: run_stage(
                                  "morning", NOW, stages, run_id=options["run_id"])) as report:
                final = scheduling._run_four_stage_report(notifier, "morning", "morning",
                    private_state_dir=stages, trial_ledger=ledger, clock=lambda: NOW)
            notifier.send.assert_called_once()
            self.assertEqual(report.call_args.kwargs["run_id"], final["run_id"])
            self.assertEqual(recorded_event(ledger)["run_id"], final["run_id"])
            self.assertTrue(recorded_event(ledger)["api_accepted"])
            self.assertEqual(final["live_acceptance_status"], "recorded_unverified")

    def test_outside_frozen_trial_dates_does_not_collect_send_or_record(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            for current in (aware("2026-09-14", "09:00"), aware("2026-09-22", "09:00")):
                notifier = Mock()
                with patch.object(scheduling, "run_portfolio_report") as report:
                    result = scheduling._run_four_stage_report(notifier, "morning", "morning",
                        private_state_dir=Path(raw) / "stages", trial_ledger=ledger, clock=lambda: current)
                self.assertEqual(result["reason"], "outside_frozen_trial_dates")
                report.assert_not_called()
                notifier.send.assert_not_called()
            self.assertEqual(len(ledger.path.read_text(encoding="utf-8").splitlines()), 1)

    def test_slow_send_records_late_actual_time_instead_of_crediting_on_time_delivery(self):
        with tempfile.TemporaryDirectory() as raw:
            stages = Path(raw) / "stages"
            report = run_stage("morning", NOW)
            ledger = make_ledger(raw, report["rule_version"])
            notifier = Mock(send=Mock(return_value=True))
            samples = iter((NOW, NOW, NOW, NOW + timedelta(minutes=11)))
            with patch.object(scheduling, "run_portfolio_report",
                              side_effect=lambda **options: run_stage(
                                  "morning", NOW, stages, run_id=options["run_id"])):
                result = scheduling._run_four_stage_report(notifier, "morning", "morning",
                    private_state_dir=stages, trial_ledger=ledger,
                    clock=lambda: next(samples, NOW + timedelta(minutes=11)))
            self.assertTrue(result["delivery"]["api_accepted"])
            self.assertEqual(recorded_event(ledger)["latency_seconds"], 660)
            self.assertEqual(ledger.summary(as_of=NOW + timedelta(minutes=11))["totals"]["complete"], 0)

    def test_retry_cannot_resend_or_rescue_a_failed_observation(self):
        with tempfile.TemporaryDirectory() as raw:
            stages = Path(raw) / "stages"
            first = run_stage("morning", NOW, stages)
            ledger = make_ledger(raw, first["rule_version"])
            observe_report(ledger, first, private_state_dir=stages, scheduled_at=NOW,
                           observed_at=NOW, clock=lambda: NOW)
            before = ledger.path.read_bytes()
            notifier = Mock()
            with patch.object(scheduling, "run_portfolio_report") as report:
                result = scheduling._run_four_stage_report(notifier, "morning", "morning",
                    private_state_dir=stages, trial_ledger=ledger, clock=lambda: NOW)
            self.assertEqual(result["reason"], "slot_already_observed")
            report.assert_not_called()
            notifier.send.assert_not_called()
            self.assertEqual(ledger.path.read_bytes(), before)

    def test_overlapping_callbacks_cannot_collect_or_send_a_second_time(self):
        with tempfile.TemporaryDirectory() as raw:
            stages = Path(raw) / "stages"
            first = run_stage("morning", NOW)
            ledger = make_ledger(raw, first["rule_version"])
            notifier = Mock(send=Mock(return_value=True))
            nested = []
            collecting = False

            def collect(**options):
                nonlocal collecting
                if not collecting:
                    collecting = True
                    # Enter a second callback before the first stage exists.
                    # Advance only the competing lock's timeout, not wall time.
                    with patch("app.workflow.live_acceptance.time_module.monotonic",
                               side_effect=(0, 6, 6, 6)):
                        nested.append(scheduling._run_four_stage_report(
                            notifier, "morning", "morning", private_state_dir=stages,
                            trial_ledger=LiveTrialLedger.load(ledger.path), clock=lambda: NOW))
                return run_stage("morning", NOW, stages, run_id=options["run_id"])

            with patch.object(scheduling, "run_portfolio_report", side_effect=collect) as report:
                result = scheduling._run_four_stage_report(
                    notifier, "morning", "morning", private_state_dir=stages,
                    trial_ledger=ledger, clock=lambda: NOW)
            report.assert_called_once()
            notifier.send.assert_called_once()
            self.assertEqual(nested[0]["status"], "failed")
            self.assertFalse(nested[0]["notification_attempted"])
            self.assertEqual(result["live_acceptance_status"], "recorded_unverified")
            self.assertEqual(len(ledger.path.read_text(encoding="utf-8").splitlines()), 3)
            self.assertFalse(ledger.path.with_suffix(".jsonl.run.lock").exists())

    def test_stale_execution_lock_is_not_removed_or_used_to_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            lock = ledger.path.with_suffix(".jsonl.run.lock")
            lock.write_text("stale-fixture-lock", encoding="utf-8")
            notifier = Mock()
            with patch.object(scheduling, "run_portfolio_report") as report, patch(
                "app.workflow.live_acceptance.time_module.monotonic", side_effect=(0, 6)
            ):
                result = scheduling._run_four_stage_report(
                    notifier, "morning", "morning", private_state_dir=Path(raw) / "stages",
                    trial_ledger=ledger, clock=lambda: NOW)
            report.assert_not_called()
            notifier.send.assert_not_called()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(lock.read_text(encoding="utf-8"), "stale-fixture-lock")
            self.assertEqual(len(ledger.path.read_text(encoding="utf-8").splitlines()), 1)

    def test_unknown_previous_delivery_does_not_retry_existing_stage(self):
        with tempfile.TemporaryDirectory() as raw:
            stages = Path(raw) / "stages"
            first = run_stage("morning", NOW, stages)
            ledger = make_ledger(raw, first["rule_version"])
            notifier = Mock()
            with patch.object(scheduling, "run_portfolio_report") as report:
                result = scheduling._run_four_stage_report(notifier, "morning", "morning",
                    private_state_dir=stages, trial_ledger=ledger, clock=lambda: NOW)
            report.assert_not_called()
            notifier.send.assert_not_called()
            self.assertEqual(result["status"], "failed")
            self.assertFalse(recorded_event(ledger)["api_accepted"])
            self.assertFalse(recorded_event(ledger)["report_valid"])

    def test_rule_change_is_recorded_and_cannot_emit_the_changed_report(self):
        with tempfile.TemporaryDirectory() as raw:
            stages = Path(raw) / "stages"
            ledger = make_ledger(raw)
            notifier = Mock()
            with patch.object(scheduling, "run_portfolio_report",
                              side_effect=lambda **options: run_stage(
                                  "morning", NOW, stages, run_id=options["run_id"])):
                result = scheduling._run_four_stage_report(notifier, "morning", "morning",
                    private_state_dir=stages, trial_ledger=ledger, clock=lambda: NOW)
            notifier.send.assert_called_once()
            self.assertIn("WAIT", notifier.send.call_args.args[0])
            self.assertIn("本时点不重跑补账", notifier.send.call_args.args[0])
            self.assertNotIn("frozen rule", notifier.send.call_args.args[0])
            self.assertEqual(result["status"], "failed")
            self.assertNotEqual(recorded_event(ledger)["rule_version"], ledger.rule_version)
            self.assertFalse(recorded_event(ledger)["report_valid"])

    def test_post_delivery_clock_failure_never_fabricates_an_observation(self):
        with tempfile.TemporaryDirectory() as raw:
            stages = Path(raw) / "stages"
            first = run_stage("morning", NOW)
            ledger = make_ledger(raw, first["rule_version"])
            notifier = Mock(send=Mock(return_value=True))
            with patch.object(scheduling, "run_portfolio_report",
                              side_effect=lambda **options: run_stage(
                                  "morning", NOW, stages, run_id=options["run_id"])):
                result = scheduling._run_four_stage_report(notifier, "morning", "morning",
                    private_state_dir=stages, trial_ledger=ledger, clock=Mock(side_effect=(NOW, NOW, NOW, None)))
            self.assertTrue(result["delivery"]["api_accepted"])
            self.assertEqual(result["live_acceptance_status"], "record_failed")
            self.assertEqual(result["status"], "partial")
            self.assertEqual(len(ledger.path.read_text(encoding="utf-8").splitlines()), 2)

    def test_fatal_collection_sends_only_safe_failure_and_still_records_the_attempt(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            notifier = Mock(send=Mock(return_value=True))
            with patch.object(scheduling, "run_portfolio_report",
                              side_effect=RuntimeError("private-fixture-value")):
                result = scheduling._run_four_stage_report(notifier, "morning", "morning",
                    private_state_dir=Path(raw) / "stages", trial_ledger=ledger, clock=lambda: NOW)
            notifier.send.assert_called_once()
            self.assertIn("WAIT", result["report"])
            self.assertIn("本时点不重跑补账", result["report"])
            self.assertNotIn("private-fixture-value", str(result))
            event = recorded_event(ledger)
            self.assertTrue(event["api_accepted"])
            self.assertFalse(event["report_valid"])
            self.assertFalse(event["data_complete"])

    def test_missing_stage_and_failed_observation_cannot_repeat_a_delivery_attempt(self):
        for outcome in (True, False, OSError("synthetic-notification-error")):
            with self.subTest(outcome=type(outcome).__name__), tempfile.TemporaryDirectory() as raw:
                ledger = make_ledger(raw)
                notifier = Mock()
                if isinstance(outcome, Exception):
                    notifier.send.side_effect = outcome
                else:
                    notifier.send.return_value = outcome
                with patch.object(scheduling, "run_portfolio_report",
                                  side_effect=RuntimeError("synthetic-collection-error")) as report, patch.object(
                    LiveTrialLedger, "append", side_effect=OSError("synthetic-observation-error")
                ):
                    first = scheduling._run_four_stage_report(
                        notifier, "morning", "morning", private_state_dir=Path(raw) / "stages",
                        trial_ledger=ledger, clock=lambda: NOW)
                    before_retry = ledger.path.read_bytes()
                    retry = scheduling._run_four_stage_report(
                        notifier, "morning", "morning", private_state_dir=Path(raw) / "stages",
                        trial_ledger=LiveTrialLedger.load(ledger.path), clock=lambda: NOW)
                notifier.send.assert_called_once()
                report.assert_called_once()
                self.assertEqual(first["live_acceptance_status"], "record_failed")
                self.assertEqual(first["delivery"]["api_accepted"], outcome is True)
                self.assertEqual(retry["reason"], "slot_already_attempted")
                self.assertFalse(retry["notification_attempted"])
                self.assertEqual(ledger.path.read_bytes(), before_retry)
                self.assertFalse(ledger.has_analysis("2026-09-15", "09:00"))
                self.assertFalse((Path(raw) / "stages" / "2026-09-15" / "morning.json").exists())

    def test_unrecorded_outcome_does_not_block_the_next_distinct_slot(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            notifier = Mock(send=Mock(return_value=True))
            with patch.object(scheduling, "run_portfolio_report",
                              side_effect=RuntimeError("synthetic-collection-error")), patch.object(
                LiveTrialLedger, "append", side_effect=OSError("synthetic-observation-error")
            ):
                for kind, current in (("morning", NOW), ("midday", NOW.replace(hour=11, minute=30))):
                    result = scheduling._run_four_stage_report(
                        notifier, kind, kind, private_state_dir=Path(raw) / "stages",
                        trial_ledger=LiveTrialLedger.load(ledger.path), clock=lambda: current)
                    self.assertEqual(result["live_acceptance_status"], "record_failed")
            self.assertEqual(notifier.send.call_count, 2)
            records = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["record_type"] for record in records], ["header", "attempt", "attempt"])
            self.assertEqual([record["event"]["stage"] for record in records[1:]], ["09:00", "11:30"])
            self.assertFalse(ledger.path.with_suffix(".jsonl.run.lock").exists())

    def test_reservation_write_failure_stops_before_collection_and_delivery(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            original = ledger.path.read_bytes()
            notifier = Mock()
            with patch.object(ledger, "reserve_attempt", side_effect=OSError("private-fixture-value")), patch.object(
                scheduling, "run_portfolio_report"
            ) as report, patch.object(ledger, "append") as append:
                result = scheduling._run_four_stage_report(
                    notifier, "morning", "morning", private_state_dir=Path(raw) / "stages",
                    trial_ledger=ledger, clock=lambda: NOW)
            report.assert_not_called()
            notifier.send.assert_not_called()
            append.assert_not_called()
            self.assertFalse(result["notification_attempted"])
            self.assertFalse(result["notification_allowed"])
            self.assertNotIn("private-fixture-value", str(result))
            self.assertEqual(ledger.path.read_bytes(), original)

    def test_presend_clock_cannot_precede_persisted_stage_even_after_scheduler_start(self):
        for use_ledger in (False, True):
            for evidence_minute in (1, 3):
                with self.subTest(ledger=use_ledger, evidence_minute=evidence_minute), tempfile.TemporaryDirectory() as raw:
                    stages = Path(raw) / "stages"
                    generated = NOW + timedelta(minutes=evidence_minute)
                    persisted = NOW + timedelta(minutes=4)
                    before_send = NOW + timedelta(minutes=2)
                    first = run_stage("morning", generated)
                    ledger = make_ledger(raw, first["rule_version"]) if use_ledger else None
                    notifier = Mock(send=Mock(return_value=True))
                    def collect(**options):
                        first["run_id"] = options.get("run_id", first["run_id"])
                        first["stage_context"]["persistence"] = StageStore(
                            stages, clock=lambda: persisted).record(first["stage_context"]["evidence"])
                        return first
                    samples = iter((NOW, NOW, before_send) if use_ledger else (NOW, before_send))
                    with patch.object(scheduling, "run_portfolio_report", side_effect=collect):
                        result = scheduling._run_four_stage_report(
                            notifier, "morning", "morning", private_state_dir=stages, trial_ledger=ledger,
                            clock=lambda: next(samples, before_send))
                    notifier.send.assert_not_called()
                    self.assertFalse(result["notification_attempted"])
                    self.assertFalse(result["notification_allowed"])
                    self.assertEqual(result["status"], "failed")
                    if ledger is not None:
                        self.assertFalse(recorded_event(ledger)["api_accepted"])
                        self.assertFalse(recorded_event(ledger)["report_valid"])

    def test_expired_collection_records_failed_slot_without_sending(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            notifier = Mock()
            late = NOW + timedelta(minutes=11)
            samples = iter((NOW, NOW, late))
            with patch.object(scheduling, "run_portfolio_report", side_effect=lambda **options: {
                "status": "partial", "report": "synthetic WAIT", "notified": False,
                "run_id": options["run_id"], "report_kind": options["report_kind"],
            }):
                result = scheduling._run_four_stage_report(notifier, "morning", "morning",
                    private_state_dir=Path(raw) / "stages", trial_ledger=ledger,
                    clock=lambda: next(samples, late))
            notifier.send.assert_not_called()
            self.assertEqual(result["reason"], "expired_after_analysis")
            self.assertEqual(result["live_acceptance_status"], "recorded_unverified")
            self.assertEqual(recorded_event(ledger)["latency_seconds"], 660)

    def test_slow_presend_stage_read_cannot_send_after_the_deadline(self):
        with tempfile.TemporaryDirectory() as raw:
            stages = Path(raw) / "stages"
            ledger = make_ledger(raw, run_stage("morning", NOW)["rule_version"])
            notifier = Mock(send=Mock(return_value=True))
            current = NOW
            collection_done = False
            original_load = StageStore.load_stage
            def collect(**options):
                nonlocal collection_done
                report = run_stage("morning", NOW, stages, run_id=options["run_id"])
                collection_done = True
                return report
            def load(store, day, kind):
                nonlocal current
                record = original_load(store, day, kind)
                if collection_done:
                    current = NOW + timedelta(minutes=11)
                return record
            with patch.object(scheduling, "run_portfolio_report", side_effect=collect), patch.object(
                StageStore, "load_stage", autospec=True, side_effect=load
            ):
                result = scheduling._run_four_stage_report(
                    notifier, "morning", "morning", private_state_dir=stages, trial_ledger=ledger,
                    clock=lambda: current)
            self.assertEqual(notifier.send.call_count, 0)
            self.assertEqual(result["reason"], "expired_after_analysis")
            self.assertFalse(result["notification_attempted"])
            self.assertEqual(result["live_acceptance_status"], "recorded_unverified")

    def test_ledger_failure_precedes_notifier_initialization(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(scheduling, "FeishuNotifier") as factory:
            with self.assertRaises((OSError, ValueError)):
                scheduling.register_four_stage_jobs(_Scheduler(), private_state_dir=Path(raw) / "stages",
                    live_ledger=Path(raw) / "missing.jsonl")
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
