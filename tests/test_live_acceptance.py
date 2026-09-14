import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from app.workflow.live_acceptance import LiveTrialLedger, ReceiptEvent, SlotEvent


TZ = "Asia/Shanghai"
RULE = "live-acceptance-v1"
CALENDAR = "synthetic-cn-trading-calendar-v1"
TRADING_DATES = (
    "2026-09-14",
    "2026-09-15",
    "2026-09-16",
    "2026-09-17",
    "2026-09-18",
    "2026-09-21",
)
SOURCE_DATES = TRADING_DATES + ("2026-09-22",)


def aware(day, stage):
    return datetime.fromisoformat(f"{day}T{stage}:00+08:00")


def passing_event(day, stage, *, run_id=None, rule_version=RULE, mode="trusted_private_live", **overrides):
    data = {
        "run_id": run_id or f"run-{day}-{stage}",
        "trading_date": day,
        "stage": stage,
        "rule_version": rule_version,
        "ingested_at": aware(day, stage),
        "mode": mode,
        "storage_visibility": "private",
        "calendar_source": CALENDAR,
        "data_complete": True,
        "report_valid": True,
        "api_accepted": True,
        "chatgpt_received": True,
        "device_received": True,
        "strategy_success": True,
        "data_evidence_id": f"data-{day}-{stage}",
        "report_fingerprint": f"sha256:report-{day}-{stage}",
        "api_receipt_id": f"api-{day}-{stage}",
        "chatgpt_ack_id": f"chatgpt-{day}-{stage}",
        "device_receipt_id": f"device-{day}-{stage}",
        "strategy_evidence_id": f"strategy-{day}-{stage}",
        "review_evidence_id": f"review-{day}-{stage}",
        "position_evidence_id": f"position-{day}-{stage}",
        "wait_reason": "",
        "user_review_second_buy_ok": True,
        "no_overtrading_ok": True,
        "position_increment_validated": True,
        "latency_seconds": 12,
    }
    data.update(overrides)
    return SlotEvent(**data)


class LiveAcceptanceLedgerTest(unittest.TestCase):
    def make_ledger(self, directory):
        return LiveTrialLedger.create(
            Path(directory) / ".private" / "live-acceptance.jsonl",
            trial_start="2026-09-14",
            rule_version=RULE,
            calendar_source=CALENDAR,
            ordered_trading_dates=TRADING_DATES,
            calendar_coverage=SOURCE_DATES,
            calendar_verified=True,
            clock=lambda: aware("2026-09-14", "09:00"),
        )

    def append(self, ledger, event):
        return ledger.append(event, clock=lambda: event.ingested_at + timedelta(minutes=1))

    def append_first_five(self, ledger):
        for day in TRADING_DATES[:5]:
            for stage in ("09:00", "11:30", "14:30", "16:10"):
                self.append(ledger, passing_event(day, stage))

    def test_selects_first_five_consecutive_trading_dates_and_excludes_holidays(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            self.append_first_five(ledger)
            self.append(ledger, passing_event("2026-09-21", "09:00", run_id="late-extra"))

            summary = ledger.summary(as_of=aware("2026-09-18", "16:20"))

        self.assertEqual(summary["status"], "pending_verification")
        self.assertEqual(
            summary["trial_dates"],
            ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"],
        )
        self.assertNotIn("2026-09-19", summary["trial_dates"])
        self.assertEqual(summary["totals"], {"complete": 20, "failed": 0, "pending": 0})

    def test_missing_stage_is_pending_not_passed(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            for day in TRADING_DATES[:5]:
                for stage in ("09:00", "11:30", "14:30", "16:10"):
                    if (day, stage) != ("2026-09-16", "14:30"):
                        self.append(ledger, passing_event(day, stage))

            summary = ledger.summary(as_of=aware("2026-09-16", "16:20"))

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["totals"], {"complete": 11, "failed": 1, "pending": 8})
        self.assertIn("missed slot", summary["days"]["2026-09-16"]["slots"]["14:30"]["reasons"])

    def test_duplicate_passing_run_cannot_erase_prior_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            self.append(ledger, passing_event("2026-09-14", "09:00", run_id="dup", data_complete=False))
            with self.assertRaisesRegex(ValueError, "run_id"):
                self.append(ledger, passing_event("2026-09-14", "09:00", run_id="dup"))

            slot = ledger.summary(as_of=aware("2026-09-14", "09:02"))["days"]["2026-09-14"]["slots"]["09:00"]

        self.assertEqual(slot["status"], "failed")
        self.assertIn("data completeness missing", slot["reasons"])

    def test_rule_change_fails_frozen_trial(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            self.append(ledger, passing_event("2026-09-14", "11:30", rule_version="changed"))

            slot = ledger.summary(as_of=aware("2026-09-14", "11:32"))["days"]["2026-09-14"]["slots"]["11:30"]

        self.assertEqual(slot["status"], "failed")
        self.assertIn("rule version mismatch", slot["reasons"])

    def test_simulated_replay_and_public_synthetic_events_are_recorded_but_excluded(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            self.append(ledger, passing_event("2026-09-14", "09:00", mode="simulated"))
            self.append(ledger, passing_event("2026-09-14", "11:30", mode="replay"))
            self.append(ledger, passing_event("2026-09-14", "14:30", mode="public_synthetic"))

            slots = ledger.summary(as_of=aware("2026-09-14", "14:32"))["days"]["2026-09-14"]["slots"]

        self.assertEqual(slots["09:00"]["status"], "failed")
        self.assertEqual(slots["11:30"]["status"], "failed")
        self.assertEqual(slots["14:30"]["status"], "failed")
        self.assertIn("not trusted private live", slots["09:00"]["reasons"])

    def test_event_requires_actual_wall_clock_to_match_trading_day_and_stage(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            self.append(ledger, passing_event("2026-09-14", "16:10", ingested_at=aware("2026-09-14", "16:21")))
            ledger.append(
                passing_event("2026-09-15", "09:00", ingested_at=aware("2026-09-14", "09:00")),
                clock=lambda: aware("2026-09-15", "09:01"),
            )

            slots = ledger.summary(as_of=aware("2026-09-15", "09:02"))["days"]

        self.assertIn("ingestion wall-clock outside slot window", slots["2026-09-14"]["slots"]["16:10"]["reasons"])
        self.assertIn("ingestion wall-clock does not match trading day", slots["2026-09-15"]["slots"]["09:00"]["reasons"])

    def test_complete_wait_can_pass_but_missing_wait_evidence_is_incomplete(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            self.append(ledger, passing_event("2026-09-14", "14:30", strategy_success=False, wait_reason="complete evidence says WAIT"))
            self.append(
                ledger,
                passing_event(
                    "2026-09-14",
                    "16:10",
                    run_id="wait-missing-evidence",
                    strategy_success=False,
                    wait_reason="missing minute bar",
                    data_complete=False,
                ),
            )

            slots = ledger.summary(as_of=aware("2026-09-14", "16:12"))["days"]["2026-09-14"]["slots"]

        self.assertEqual(slots["14:30"]["status"], "pending_verification")
        self.assertEqual(slots["16:10"]["status"], "pending")
        self.assertIn("WAIT evidence incomplete", slots["16:10"]["reasons"])

    def test_user_review_and_position_quality_remain_required(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            self.append(
                ledger,
                passing_event(
                    "2026-09-14",
                    "14:30",
                    user_review_second_buy_ok=False,
                    no_overtrading_ok=False,
                    position_increment_validated=False,
                ),
            )

            reasons = ledger.summary(as_of=aware("2026-09-14", "14:32"))["days"]["2026-09-14"]["slots"]["14:30"]["reasons"]

        self.assertIn("second-buy review missing", reasons)
        self.assertIn("overtrading review missing", reasons)
        self.assertIn("position increment validation missing", reasons)

    def test_unknown_calendar_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError, "calendar"):
                LiveTrialLedger.create(
                    Path(raw) / ".private" / "live-acceptance.jsonl",
                    trial_start="2026-09-14",
                    rule_version=RULE,
                    calendar_source="",
                    ordered_trading_dates=TRADING_DATES,
                    calendar_coverage=SOURCE_DATES,
                    calendar_verified=True,
                )

    def test_rejects_future_naive_bool_timestamp_and_bool_numeric_values(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            cases = (
                ("future", {"ingested_at": aware("2026-09-14", "09:00")}, aware("2026-09-14", "08:59")),
                ("naive", {"ingested_at": datetime(2026, 9, 14, 9, 0)}, aware("2026-09-14", "09:00")),
                ("bool timestamp", {"ingested_at": True}, aware("2026-09-14", "09:00")),
                ("bool numeric", {"latency_seconds": False}, aware("2026-09-14", "09:00")),
            )
            for label, kwargs, now in cases:
                with self.subTest(label=label):
                    with self.assertRaises((TypeError, ValueError)):
                        ledger.append(passing_event("2026-09-14", "09:00", **kwargs), clock=lambda now=now: now)

    def test_private_path_guard_rejects_source_paths_and_other_worktrees(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as raw:
            with self.assertRaisesRegex(ValueError, "private"):
                LiveTrialLedger.create(
                    Path(raw) / "live-acceptance.jsonl",
                    trial_start="2026-09-14",
                    rule_version=RULE,
                    calendar_source=CALENDAR,
                    ordered_trading_dates=TRADING_DATES,
                    calendar_coverage=SOURCE_DATES,
                    calendar_verified=True,
                )

    def test_private_path_guard_rejects_tracked_or_linked_private_paths(self):
        tracked = Mock(returncode=0)
        with patch("app.utils.private_storage.subprocess.run", return_value=tracked):
            with self.assertRaisesRegex(ValueError, "tracked"):
                LiveTrialLedger.create(
                    Path.cwd() / ".private" / "tracked-live-acceptance.jsonl",
                    trial_start="2026-09-14",
                    rule_version=RULE,
                    calendar_source=CALENDAR,
                    ordered_trading_dates=TRADING_DATES,
                    calendar_coverage=SOURCE_DATES,
                    calendar_verified=True,
                    clock=lambda: aware("2026-09-14", "09:00"),
                )
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "target"
            target.mkdir()
            link = Path(raw) / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            with self.assertRaisesRegex(ValueError, "links"):
                LiveTrialLedger.create(
                    link / "live-acceptance.jsonl",
                    trial_start="2026-09-14",
                    rule_version=RULE,
                    calendar_source=CALENDAR,
                    ordered_trading_dates=TRADING_DATES,
                    calendar_coverage=SOURCE_DATES,
                    calendar_verified=True,
                    clock=lambda: aware("2026-09-14", "09:00"),
                )
        with tempfile.TemporaryDirectory() as raw:
            other = Path(raw)
            (other / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "private"):
                LiveTrialLedger.create(
                    other / "live-acceptance.jsonl",
                    trial_start="2026-09-14",
                    rule_version=RULE,
                    calendar_source=CALENDAR,
                    ordered_trading_dates=TRADING_DATES,
                    calendar_coverage=SOURCE_DATES,
                    calendar_verified=True,
                    clock=lambda: aware("2026-09-14", "09:00"),
                )

    def test_append_only_records_have_header_hash_chain_and_payload_is_not_mutated(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            event = passing_event("2026-09-14", "09:00")
            self.append(ledger, event)
            self.append(ledger, passing_event("2026-09-14", "11:30"))

            lines = [
                json.loads(line)
                for line in (Path(raw) / ".private" / "live-acceptance.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(lines[0]["record_type"], "header")
        self.assertEqual(lines[0]["header"]["trial_dates"], list(TRADING_DATES[:5]))
        self.assertEqual(lines[1]["prev_hash"], lines[0]["entry_hash"])
        self.assertEqual(lines[2]["prev_hash"], lines[1]["entry_hash"])
        self.assertEqual(event.data_complete, True)
        self.assertNotIn("passed", lines[1]["event"])

    def test_create_is_exclusive_and_load_uses_persisted_frozen_header(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / ".private" / "live-acceptance.jsonl"
            created = self.make_ledger(raw)
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                LiveTrialLedger.create(
                    path,
                    trial_start="2026-09-15",
                    rule_version="changed",
                    calendar_source="changed",
                    ordered_trading_dates=TRADING_DATES[1:] + ("2026-09-22",),
                    calendar_coverage=SOURCE_DATES,
                    calendar_verified=True,
                )

            loaded = LiveTrialLedger.load(path)

        self.assertEqual(created.trial_dates, loaded.trial_dates)
        self.assertEqual(loaded.rule_version, RULE)
        self.assertEqual(loaded.calendar_source, CALENDAR)

    def test_backfilled_events_cannot_pass_with_late_recorded_clock(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            for day in TRADING_DATES[:5]:
                for stage in ("09:00", "11:30", "14:30", "16:10"):
                    ledger.append(
                        passing_event(day, stage),
                        clock=lambda: aware("2026-09-21", "16:10"),
                    )

            summary = ledger.summary(as_of=aware("2026-09-21", "16:20"))

        self.assertEqual(summary["status"], "failed")
        self.assertIn("record wall-clock does not match trading day", summary["days"]["2026-09-14"]["slots"]["09:00"]["reasons"])

    def test_missing_receipt_is_pending_until_receipt_event_supplies_ids(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            event = passing_event(
                "2026-09-14",
                "09:00",
                chatgpt_received=False,
                device_received=False,
                chatgpt_ack_id=None,
                device_receipt_id=None,
            )
            ledger.append(event, clock=lambda: aware("2026-09-14", "09:01"))
            before = ledger.summary(as_of=aware("2026-09-14", "09:03"))["days"]["2026-09-14"]["slots"]["09:00"]
            ledger.append_receipt(
                ReceiptEvent(
                    run_id=event.run_id,
                    trading_date="2026-09-14",
                    stage="09:00",
                    chatgpt_ack_id="late-chatgpt-ack",
                    device_receipt_id="late-device-receipt",
                    receipt_observed_at=aware("2026-09-14", "09:04"),
                ),
                clock=lambda: aware("2026-09-14", "09:04"),
            )
            after = ledger.summary(as_of=aware("2026-09-14", "09:05"))["days"]["2026-09-14"]["slots"]["09:00"]

        self.assertEqual(before["status"], "pending")
        self.assertIn("ChatGPT receipt pending", before["reasons"])
        self.assertEqual(after["status"], "pending_verification")

    def test_evidence_references_are_required_not_just_true_statuses(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            ledger.append(
                passing_event(
                    "2026-09-14",
                    "11:30",
                    report_fingerprint=None,
                    api_receipt_id=None,
                ),
                clock=lambda: aware("2026-09-14", "11:31"),
            )

            slot = ledger.summary(as_of=aware("2026-09-14", "11:32"))["days"]["2026-09-14"]["slots"]["11:30"]

        self.assertEqual(slot["status"], "failed")
        self.assertIn("report fingerprint missing", slot["reasons"])
        self.assertIn("API receipt evidence missing", slot["reasons"])

    def test_calendar_rejects_weekends_and_unverified_calendar_cannot_pass(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError, "weekend"):
                LiveTrialLedger.create(
                    Path(raw) / ".private" / "bad-calendar.jsonl",
                    trial_start="2026-09-14",
                    rule_version=RULE,
                    calendar_source=CALENDAR,
                    ordered_trading_dates=("2026-09-14", "2026-09-19", "2026-09-21", "2026-09-22", "2026-09-23"),
                    calendar_coverage=SOURCE_DATES,
                    calendar_verified=True,
                )
        with tempfile.TemporaryDirectory() as raw:
            ledger = LiveTrialLedger.create(
                Path(raw) / ".private" / "unverified-calendar.jsonl",
                trial_start="2026-09-14",
                rule_version=RULE,
                calendar_source=CALENDAR,
                ordered_trading_dates=TRADING_DATES,
                calendar_coverage=SOURCE_DATES,
                calendar_verified=False,
                clock=lambda: aware("2026-09-14", "09:00"),
            )
            self.append_first_five(ledger)

            summary = ledger.summary(as_of=aware("2026-09-18", "16:20"))

        self.assertEqual(summary["status"], "failed")
        self.assertIn("calendar not independently verified", summary["reasons"])

    def test_corrupt_or_oversized_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            self.append(ledger, passing_event("2026-09-14", "09:00"))
            path = Path(raw) / ".private" / "live-acceptance.jsonl"
            path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "newline"):
                LiveTrialLedger.load(path)

    def test_concurrent_appends_preserve_hash_chain_without_losing_records(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = self.make_ledger(raw)
            errors = []
            threads = []
            def worker(event):
                try:
                    ledger.append(
                        event,
                        # Concurrent writers share one wall clock. Their
                        # observations may be older than the append time.
                        clock=lambda: aware("2026-09-14", "16:11"),
                    )
                except Exception as exc:  # pragma: no cover - reported below
                    errors.append(exc)

            for stage in ("09:00", "11:30", "14:30", "16:10"):
                event = passing_event("2026-09-14", stage)
                thread = threading.Thread(target=worker, args=(event,))
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()

            records = LiveTrialLedger.load(Path(raw) / ".private" / "live-acceptance.jsonl")._records()

        self.assertEqual(errors, [])
        self.assertEqual(len(records), 5)


if __name__ == "__main__":
    unittest.main()
