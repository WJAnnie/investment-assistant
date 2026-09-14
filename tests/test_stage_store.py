"""All records in this module are synthetic and offline."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.workflow.stage_store import StageStore
from app.workflow.stage_store import _digest
from app.workflow.stage_evidence import _fingerprint
from tests.test_stage_evidence import (
    MORNING, MIDDAY, CLOSING, CN_CLOSE, _evidence, _holding_snapshot, _snapshot,
)


def evidence(kind="morning", cutoff=MORNING, price="10", as_of=None):
    return _evidence(_snapshot((_holding_snapshot("A", "DEMO", price=price,
                                                as_of=as_of or cutoff),)),
                     cutoff, report_kind=kind)


class StageStoreTests(unittest.TestCase):
    def forecast(self, observation):
        holding = observation["holdings"][0]
        return {
            "target": {key: holding[key] for key in (
                "account_id", "code", "market", "valuation_mode",
                "valuation_source", "holding_config_fingerprint")},
            "explicit": True, "prospective": True, "direction": "up",
            "start": MORNING.isoformat(), "deadline": CN_CLOSE.isoformat(),
            "start_price": holding["price"], "rule_version": observation["rule_version"],
            "cutoff": observation["cutoff"],
        }

    def test_prospective_forecast_round_trip_uses_writer_time(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StageStore(Path(raw), clock=lambda: MORNING)
            observation = evidence()
            forecast = self.forecast(observation)
            store.record(observation, forecasts=[forecast])
            self.assertEqual(store.load_forecasts("2026-09-15"),
                             [{**forecast, "recorded_at": MORNING.isoformat()}])

    def test_rehashed_nested_forecast_time_cannot_disagree_with_record(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StageStore(Path(raw), clock=lambda: MORNING)
            observation = evidence()
            store.record(observation, forecasts=[self.forecast(observation)])
            path = Path(raw) / "2026-09-15" / "morning.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record["forecasts"][0]["recorded_at"] = MIDDAY.isoformat()
            record["record_fingerprint"] = _digest({key: value for key, value in record.items()
                                                     if key != "record_fingerprint"})
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "forecast"):
                store.load_forecasts("2026-09-15")

    def test_forecast_origin_is_revalidated_instead_of_trusting_input_status(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StageStore(Path(raw), clock=lambda: MORNING)
            observation = evidence()
            observation["holdings"][0]["valuation_price"] = "999"
            observation["evidence_fingerprint"] = _fingerprint({
                key: value for key, value in observation.items() if key != "evidence_fingerprint"})
            self.assertEqual(observation["holdings"][0]["input_status"], "valid")
            with self.assertRaisesRegex(ValueError, "starting price"):
                store.record(observation, forecasts=[self.forecast(observation)])

    def test_malformed_forecast_fields_raise_validation_error(self):
        for key, value in (("start", None), ("deadline", []), ("target", [])):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as raw:
                store = StageStore(Path(raw), clock=lambda: MORNING)
                observation = evidence()
                with self.assertRaises(ValueError):
                    store.record(observation, forecasts=[{**self.forecast(observation), key: value}])

    def test_first_morning_is_persisted_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StageStore(Path(raw), clock=lambda: MORNING)
            first = evidence()
            self.assertEqual(store.record(first)["status"], "stored")
            self.assertEqual(store.record(evidence(price="99"))["status"], "already_recorded")
            loaded = store.load_morning("2026-09-15")
            self.assertEqual(loaded, first)
            self.assertEqual(StageStore(Path(raw)).load_morning("2026-09-15"), first)

    def test_missing_morning_is_explicit_and_date_cannot_traverse(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StageStore(Path(raw))
            self.assertIsNone(store.load_morning("2026-09-15"))
            for invalid in ("../2026-09-15", "20260915", [], None):
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    store.load_morning(invalid)

    def test_backdated_morning_cannot_be_saved_by_injecting_evidence_times(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StageStore(Path(raw), clock=lambda: MIDDAY)
            with self.assertRaisesRegex(ValueError, "window"):
                store.record(evidence())
            self.assertIsNone(store.load_morning("2026-09-15"))

    def test_future_malformed_or_tampered_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StageStore(Path(raw), clock=lambda: MORNING)
            invalid = evidence()
            invalid["holdings"][0]["price"] = "99"
            for item in (invalid, evidence("midday", MIDDAY), None):
                with self.subTest(item=type(item).__name__), self.assertRaises(ValueError):
                    store.record(item)

    def test_corrupted_duplicate_json_and_oversized_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StageStore(Path(raw), clock=lambda: MORNING)
            store.record(evidence())
            path = Path(raw) / "2026-09-15" / "morning.json"
            saved = path.read_bytes()
            for data in (b'{"schema":1,"schema":2}', b"x" * 2_000_001, saved.replace(b'"10"', b'"99"')):
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    store.load_morning("2026-09-15")

    def test_public_and_unknown_actions_fail_before_creating_state(self):
        for environment in ({"GITHUB_ACTIONS": "true"}, {"GITHUB_ACTIONS": ""},
                            {"GITHUB_ACTIONS": "false", "GITHUB_REPOSITORY_VISIBILITY": "private"}):
            with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, "private"):
                    StageStore(Path(raw) / "state")
                self.assertFalse((Path(raw) / "state").exists())

    def test_source_and_other_worktrees_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "private"):
            StageStore(Path.cwd() / "data" / "stage-records")
        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "private"):
                StageStore(Path(raw) / "state")

    def test_paths_are_rechecked_after_store_construction(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StageStore(Path(raw), clock=lambda: MORNING)
            (Path(raw) / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "private"):
                store.record(evidence())

    def test_report_stage_must_match_its_real_wall_clock_window(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StageStore(Path(raw), clock=lambda: MIDDAY)
            forged = evidence("morning", MIDDAY)
            with self.assertRaisesRegex(ValueError, "window"):
                store.record(forged)

    def test_no_forecast_is_reconstructed_from_closing_prices(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StageStore(Path(raw), clock=lambda: CLOSING)
            store.record(evidence("closing", CLOSING, as_of=CN_CLOSE))
            self.assertEqual(store.load_forecasts("2026-09-15"), [])


if __name__ == "__main__":
    unittest.main()
