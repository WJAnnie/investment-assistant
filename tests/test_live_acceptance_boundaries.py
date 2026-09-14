"""Synthetic consistency checks, never evidence of a real account trial."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.workflow import live_acceptance as acceptance
from tests.test_live_acceptance import (
    CALENDAR, RULE, SOURCE_DATES, TRADING_DATES,
    aware, passing_event,
)
from tests import test_live_acceptance as fixtures


def make_ledger(directory):
    return fixtures.LiveAcceptanceLedgerTest().make_ledger(directory)


def rewrite(path, records):
    """An attacker can rehash; schema/relationship checks must still reject."""
    previous = ""
    for record in records:
        record["prev_hash"] = previous
        record.pop("entry_hash", None)
        record["entry_hash"] = acceptance._entry_hash(record)
        previous = record["entry_hash"]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


class LiveAcceptanceBoundaryTests(unittest.TestCase):
    def test_attempt_survives_reload_without_claiming_analysis_or_delivery(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            reserved = ledger.reserve_attempt("synthetic-attempt", "2026-09-14", "09:00",
                                              clock=lambda: aware("2026-09-14", "09:00"))
            ledger = acceptance.LiveTrialLedger.load(ledger.path)
            self.assertTrue(ledger.has_attempt("2026-09-14", "09:00"))
            self.assertFalse(ledger.has_analysis("2026-09-14", "09:00"))
            self.assertEqual(set(reserved["event"]), {"run_id", "trading_date", "stage"})
            for at, status in (("09:05", "pending"), ("09:11", "failed")):
                summary = ledger.summary(as_of=aware("2026-09-14", at))
                slot = summary["days"]["2026-09-14"]["slots"]["09:00"]
                self.assertEqual(slot["status"], status)
                self.assertIn("attempt outcome missing", slot["reasons"])
                self.assertEqual(summary["totals"]["complete"], 0)

    def test_attempt_slot_and_run_are_unique_even_across_instances(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            ledger.reserve_attempt("synthetic-attempt", "2026-09-14", "09:00",
                                   clock=lambda: aware("2026-09-14", "09:00"))
            before = ledger.path.read_bytes()
            for run, stage in (("synthetic-other", "09:00"), ("synthetic-attempt", "11:30")):
                with self.subTest(run=run, stage=stage), self.assertRaisesRegex(ValueError, "already recorded"):
                    acceptance.LiveTrialLedger.load(ledger.path).reserve_attempt(
                        run, "2026-09-14", stage, clock=lambda: aware("2026-09-14", stage))
            self.assertEqual(ledger.path.read_bytes(), before)

    def test_analysis_must_match_reserved_identity_and_time(self):
        for run, stage, ingested in (("different-run", "09:00", "09:02"),
                                     ("synthetic-attempt", "11:30", "11:30"),
                                     ("synthetic-attempt", "09:00", "09:00")):
            with self.subTest(run=run, stage=stage, ingested=ingested), tempfile.TemporaryDirectory() as raw:
                ledger = make_ledger(raw)
                ledger.reserve_attempt("synthetic-attempt", "2026-09-14", "09:00",
                                       clock=lambda: aware("2026-09-14", "09:01"))
                before = ledger.path.read_bytes()
                event = passing_event("2026-09-14", stage, run_id=run, ingested_at=aware("2026-09-14", ingested))
                with self.assertRaisesRegex(ValueError, "reserved|reservation"):
                    ledger.append(event, clock=lambda: aware("2026-09-14", "11:30"))
                self.assertEqual(ledger.path.read_bytes(), before)
                ledger.append(passing_event("2026-09-14", "09:00", run_id="synthetic-attempt",
                                            ingested_at=aware("2026-09-14", "09:02")),
                              clock=lambda: aware("2026-09-14", "09:02"))
                self.assertTrue(ledger.has_analysis("2026-09-14", "09:00"))

    def test_attempt_cannot_replace_analysis_or_serve_as_a_receipt_parent(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            ledger.reserve_attempt("synthetic-attempt", "2026-09-14", "09:00",
                                   clock=lambda: aware("2026-09-14", "09:00"))
            with self.assertRaisesRegex(ValueError, "existing analysis"):
                ledger.append_receipt(acceptance.ReceiptEvent(
                    "synthetic-attempt", "2026-09-14", "09:00", "synthetic-ack", None,
                    aware("2026-09-14", "09:01")), clock=lambda: aware("2026-09-14", "09:01"))
            ledger.append(passing_event("2026-09-14", "11:30"),
                          clock=lambda: aware("2026-09-14", "11:30"))
            with self.assertRaisesRegex(ValueError, "already recorded"):
                ledger.reserve_attempt("new-attempt", "2026-09-14", "11:30",
                                       clock=lambda: aware("2026-09-14", "11:30"))

    def test_rehashed_duplicate_or_claimed_success_attempt_is_rejected(self):
        for mutation in ("duplicate", "claim", "run-mismatch", "late"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                ledger = make_ledger(raw)
                ledger.reserve_attempt("synthetic-attempt", "2026-09-14", "09:00",
                                       clock=lambda: aware("2026-09-14", "09:00"))
                ledger.append(passing_event("2026-09-14", "09:00", run_id="synthetic-attempt"),
                              clock=lambda: aware("2026-09-14", "09:01"))
                records = ledger._records()
                if mutation == "duplicate":
                    records.insert(2, json.loads(json.dumps(records[1])))
                elif mutation == "claim":
                    records[1]["event"]["api_accepted"] = True
                elif mutation == "run-mismatch":
                    records[2]["event"]["run_id"] = "different-attempt"
                else:
                    records[1]["recorded_at"] = aware("2026-09-14", "09:11").isoformat()
                rewrite(ledger.path, records)
                with self.assertRaises(ValueError):
                    acceptance.LiveTrialLedger.load(ledger.path)

    def test_attempt_must_be_reserved_in_its_frozen_live_window(self):
        for day, at in (("2026-09-14", "08:59"), ("2026-09-14", "09:11"), ("2026-09-21", "09:00")):
            with self.subTest(day=day, at=at), tempfile.TemporaryDirectory() as raw:
                ledger = make_ledger(raw)
                before = ledger.path.read_bytes()
                with self.assertRaises(ValueError):
                    ledger.reserve_attempt("synthetic-attempt", day, "09:00", clock=lambda: aware(day, at))
                self.assertEqual(ledger.path.read_bytes(), before)

    def test_competing_attempt_reservations_only_write_one_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            def reserve(index):
                try:
                    acceptance.LiveTrialLedger.load(ledger.path).reserve_attempt(
                        f"synthetic-attempt-{index}", "2026-09-14", "09:00",
                        clock=lambda: aware("2026-09-14", "09:00"))
                    return True
                except ValueError:
                    return False
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(reserve, range(2)))
            self.assertEqual(sum(results), 1)
            self.assertEqual([record["record_type"] for record in ledger._records()], ["header", "attempt"])

    def test_local_true_flags_never_certify_real_five_day_acceptance(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            fixtures.LiveAcceptanceLedgerTest().append_first_five(ledger)
            summary = ledger.summary(as_of=aware("2026-09-18", "16:20"))
        self.assertEqual(summary["status"], "pending_verification")
        self.assertEqual(summary["evidence_status"], "complete")
        self.assertFalse(summary["independently_verified"])
        self.assertIn("not attestation", summary["trust_boundary"])

    def test_missing_receipts_and_incomplete_wait_expire(self):
        for kwargs in (
            {"chatgpt_received": False, "chatgpt_ack_id": None},
            {"wait_reason": "missing input", "data_complete": False},
        ):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as raw:
                ledger = make_ledger(raw)
                ledger.append(passing_event("2026-09-14", "09:00", **kwargs),
                              clock=lambda: aware("2026-09-14", "09:01"))
                slot = ledger.summary(as_of=aware("2026-09-14", "09:11"))["days"]["2026-09-14"]["slots"]["09:00"]
                self.assertEqual(slot["status"], "failed")
                self.assertIn("evidence deadline expired", slot["reasons"])

    def test_independent_receipt_channels_accumulate_without_erasing_each_other(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            event = passing_event("2026-09-14", "09:00", chatgpt_received=False,
                                  device_received=False, chatgpt_ack_id=None, device_receipt_id=None)
            ledger.append(event, clock=lambda: aware("2026-09-14", "09:01"))
            for chatgpt, device, stage in (("synthetic-chatgpt", None, "09:02"),
                                         (None, "synthetic-device", "09:03")):
                ledger.append_receipt(acceptance.ReceiptEvent(
                    event.run_id, event.trading_date, event.stage, chatgpt, device,
                    aware("2026-09-14", stage)), clock=lambda: aware("2026-09-14", stage))
            slot = ledger.summary(as_of=aware("2026-09-14", "09:05"))["days"]["2026-09-14"]["slots"]["09:00"]
            self.assertEqual(slot["status"], "pending_verification")
            self.assertEqual(slot["evidence_status"], "complete")
            with self.assertRaisesRegex(ValueError, "already recorded"):
                ledger.append_receipt(acceptance.ReceiptEvent(
                    event.run_id, event.trading_date, event.stage, "duplicate", None,
                    aware("2026-09-14", "09:04")), clock=lambda: aware("2026-09-14", "09:04"))

    def test_receipt_cannot_precede_its_analysis_record(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            event = passing_event("2026-09-14", "09:00", chatgpt_received=False, chatgpt_ack_id=None)
            ledger.append(event, clock=lambda: aware("2026-09-14", "09:02"))
            with self.assertRaisesRegex(ValueError, "precede"):
                ledger.append_receipt(acceptance.ReceiptEvent(
                    event.run_id, event.trading_date, event.stage, "synthetic", None,
                    aware("2026-09-14", "09:01")), clock=lambda: aware("2026-09-14", "09:03"))

    def test_calendar_cannot_skip_a_day_present_in_its_coverage(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "trial.jsonl"
            with self.assertRaisesRegex(ValueError, "calendar"):
                acceptance.LiveTrialLedger.create(
                    path, trial_start=TRADING_DATES[0], rule_version=RULE,
                    calendar_source=CALENDAR,
                    ordered_trading_dates=(TRADING_DATES[0], *TRADING_DATES[2:], SOURCE_DATES[-1]),
                    calendar_coverage=SOURCE_DATES, calendar_verified=True,
                    clock=lambda: aware("2026-09-14", "09:00"))
            self.assertFalse(path.exists())

    def test_late_initialization_rejected_before_creating_file(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "trial.jsonl"
            with self.assertRaisesRegex(ValueError, "start"):
                acceptance.LiveTrialLedger.create(
                    path, trial_start=TRADING_DATES[0], rule_version=RULE,
                    calendar_source=CALENDAR, ordered_trading_dates=TRADING_DATES,
                    calendar_coverage=SOURCE_DATES, calendar_verified=True,
                    clock=lambda: aware("2026-09-14", "09:01"))
            self.assertFalse(path.exists())

    def test_rehashed_header_cannot_expand_delay_or_remove_calendar_coverage(self):
        for key, value in (("max_slot_delay_seconds", 99999), ("calendar_coverage", list(TRADING_DATES[1:]))):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as raw:
                ledger = make_ledger(raw)
                records = ledger._records()
                records[0]["header"][key] = value
                rewrite(ledger.path, records)
                with self.assertRaises(ValueError):
                    acceptance.LiveTrialLedger.load(ledger.path)

    def test_rehashed_duplicate_header_or_run_is_not_accepted(self):
        for duplicate in ("header", "analysis"):
            with self.subTest(duplicate=duplicate), tempfile.TemporaryDirectory() as raw:
                ledger = make_ledger(raw)
                ledger.append(passing_event("2026-09-14", "09:00"),
                              clock=lambda: aware("2026-09-14", "09:01"))
                records = ledger._records()
                records.append(json.loads(json.dumps(records[0 if duplicate == "header" else 1])))
                rewrite(ledger.path, records)
                with self.assertRaises(ValueError):
                    acceptance.LiveTrialLedger.load(ledger.path)

    def test_header_access_returns_an_isolated_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            ledger.header["trial_dates"].pop()
            ledger.header["rule_version"] = "mutated"
            self.assertEqual(ledger.header["rule_version"], RULE)
            self.assertEqual(len(ledger.header["trial_dates"]), 5)

    def test_append_revalidates_private_location(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            (Path(raw) / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "private"):
                ledger.append(passing_event("2026-09-14", "09:00"),
                              clock=lambda: aware("2026-09-14", "09:01"))

    def test_append_checks_resulting_total_size_before_writing(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            saved = ledger.path.read_bytes()
            with patch.object(acceptance, "MAX_FILE_BYTES", len(saved) + 100):
                with self.assertRaisesRegex(ValueError, "size cap"):
                    ledger.append(passing_event("2026-09-14", "09:00"),
                                  clock=lambda: aware("2026-09-14", "09:01"))
            self.assertEqual(ledger.path.read_bytes(), saved)

    def test_partial_os_writes_do_not_truncate_jsonl(self):
        original_write = os.write
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            with patch.object(acceptance.os, "write", side_effect=lambda fd, data: original_write(fd, data[:3])):
                ledger.append(passing_event("2026-09-14", "09:00"),
                              clock=lambda: aware("2026-09-14", "09:01"))
            self.assertEqual(len(acceptance.LiveTrialLedger.load(ledger.path)._records()), 2)

    def test_false_clock_is_invalid_instead_of_silently_using_now(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            with self.assertRaises(TypeError):
                ledger.summary(as_of=False)

    def test_noncallable_or_invalid_clock_never_writes_evidence(self):
        invalid_clocks = (
            False, 0, "", [], {},
            lambda: None, lambda: False, lambda: "2026-09-14T09:00:00+08:00",
            lambda: datetime(2026, 9, 14, 9),
            lambda: datetime.max.replace(tzinfo=timezone.utc),
        )
        for operation in ("create", "analysis", "receipt", "attempt"):
            for index, clock in enumerate(invalid_clocks):
                with self.subTest(operation=operation, clock=index), tempfile.TemporaryDirectory() as raw:
                    ledger = make_ledger(raw)
                    event = passing_event("2026-09-14", "09:00",
                                          chatgpt_received=False, chatgpt_ack_id=None)
                    if operation == "receipt":
                        ledger.append(event, clock=lambda: aware("2026-09-14", "09:01"))
                    before = ledger.path.read_bytes()
                    destination = Path(raw) / "new-trial.jsonl"
                    with self.assertRaisesRegex((TypeError, ValueError), "clock"):
                        if operation == "create":
                            acceptance.LiveTrialLedger.create(
                                destination, trial_start=TRADING_DATES[0], rule_version=RULE,
                                calendar_source=CALENDAR, ordered_trading_dates=TRADING_DATES,
                                calendar_coverage=SOURCE_DATES, clock=clock)
                        elif operation == "analysis":
                            ledger.append(event, clock=clock)
                        elif operation == "attempt":
                            ledger.reserve_attempt("synthetic-attempt", event.trading_date, event.stage, clock=clock)
                        else:
                            ledger.append_receipt(acceptance.ReceiptEvent(
                                event.run_id, event.trading_date, event.stage,
                                "synthetic-ack", None, aware("2026-09-14", "09:02")),
                                clock=clock)
                    self.assertEqual(ledger.path.read_bytes(), before)
                    self.assertFalse(destination.exists())

    def test_falsey_callable_is_sampled_and_not_replaced_by_system_clock(self):
        class FalseyClock:
            def __init__(self, value):
                self.value = value

            def __bool__(self):
                return False

            def __call__(self):
                return self.value

        with tempfile.TemporaryDirectory() as raw:
            ledger = acceptance.LiveTrialLedger.create(
                Path(raw) / "trial.jsonl", trial_start=TRADING_DATES[0], rule_version=RULE,
                calendar_source=CALENDAR, ordered_trading_dates=TRADING_DATES,
                calendar_coverage=SOURCE_DATES,
                clock=FalseyClock(aware("2026-09-14", "09:00")))
            event = passing_event("2026-09-14", "09:00", chatgpt_received=False, chatgpt_ack_id=None)
            analysis = ledger.append(event, clock=FalseyClock(aware("2026-09-14", "09:01")))
            receipt = ledger.append_receipt(acceptance.ReceiptEvent(
                event.run_id, event.trading_date, event.stage, "synthetic-ack", None,
                aware("2026-09-14", "09:02")), clock=FalseyClock(aware("2026-09-14", "09:02")))
            self.assertEqual(ledger.header["created_at"], aware("2026-09-14", "09:00").isoformat())
            self.assertEqual(analysis["recorded_at"], aware("2026-09-14", "09:01").isoformat())
            self.assertEqual(receipt["recorded_at"], aware("2026-09-14", "09:02").isoformat())

    def test_clock_exception_does_not_expose_its_message(self):
        def failing_clock():
            raise RuntimeError("synthetic-private-value-must-not-appear")

        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            before = ledger.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "clock") as raised:
                ledger.append(passing_event("2026-09-14", "09:00"), clock=failing_clock)
            self.assertNotIn("synthetic-private-value", str(raised.exception))
            self.assertTrue(raised.exception.__suppress_context__)
            self.assertEqual(ledger.path.read_bytes(), before)

    def test_writer_clock_cannot_move_backwards_between_records(self):
        for operation in ("analysis", "receipt"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as raw:
                ledger = make_ledger(raw)
                event = passing_event("2026-09-14", "09:00", chatgpt_received=False,
                                      chatgpt_ack_id=None, device_received=False, device_receipt_id=None)
                ledger.append(event, clock=lambda: aware("2026-09-14", "09:01"))
                ledger.append_receipt(acceptance.ReceiptEvent(
                    event.run_id, event.trading_date, event.stage, "synthetic-ack", None,
                    aware("2026-09-14", "09:03")), clock=lambda: aware("2026-09-14", "09:03"))
                before = ledger.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "backwards"):
                    if operation == "analysis":
                        ledger.append(passing_event(
                            "2026-09-14", "09:00", run_id="synthetic-second-run",
                            chatgpt_received=False, chatgpt_ack_id=None,
                            device_received=False, device_receipt_id=None),
                            clock=lambda: aware("2026-09-14", "09:02"))
                    else:
                        ledger.append_receipt(acceptance.ReceiptEvent(
                            event.run_id, event.trading_date, event.stage, None, "synthetic-device",
                            aware("2026-09-14", "09:02")),
                            clock=lambda: aware("2026-09-14", "09:02"))
                self.assertEqual(ledger.path.read_bytes(), before)

    def test_rehashed_record_still_rejects_backwards_writer_time(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            event = passing_event("2026-09-14", "09:00", chatgpt_received=False,
                                  chatgpt_ack_id=None, device_received=False, device_receipt_id=None)
            ledger.append(event, clock=lambda: aware("2026-09-14", "09:01"))
            for chatgpt, device, minute in (("synthetic-ack", None, "09:02"),
                                          (None, "synthetic-device", "09:03")):
                ledger.append_receipt(acceptance.ReceiptEvent(
                    event.run_id, event.trading_date, event.stage, chatgpt, device,
                    aware("2026-09-14", minute)), clock=lambda: aware("2026-09-14", minute))
            records = ledger._records()
            records[-1]["recorded_at"] = aware("2026-09-14", "09:01").isoformat()
            records[-1]["event"]["receipt_observed_at"] = records[-1]["recorded_at"]
            rewrite(ledger.path, records)
            with self.assertRaisesRegex(ValueError, "backwards"):
                acceptance.LiveTrialLedger.load(ledger.path)

    def test_writer_clock_is_sampled_under_the_append_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = make_ledger(raw)
            event = passing_event("2026-09-14", "09:00",
                                  chatgpt_received=False, chatgpt_ack_id=None)
            samples = []

            def locked_clock():
                self.assertTrue(ledger.path.with_suffix(ledger.path.suffix + ".lock").exists())
                samples.append(aware("2026-09-14", "09:02"))
                return samples[-1]

            ledger.append(event, clock=locked_clock)
            ledger.append_receipt(acceptance.ReceiptEvent(
                event.run_id, event.trading_date, event.stage, "synthetic-ack", None,
                aware("2026-09-14", "09:02")), clock=locked_clock)
            self.assertEqual(len(samples), 2)

    def test_evidence_ids_must_be_text_not_truthy_objects(self):
        for value in (True, 123, ["fake"], {"fake": "id"}, "  "):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as raw:
                ledger = make_ledger(raw)
                with self.assertRaises((TypeError, ValueError)):
                    ledger.append(passing_event("2026-09-14", "09:00", api_receipt_id=value),
                                  clock=lambda: aware("2026-09-14", "09:01"))


if __name__ == "__main__":
    unittest.main()
