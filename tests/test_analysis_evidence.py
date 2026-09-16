import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone

from app.decision.gates import evaluate_gates
from app.domain.evidence import (
    MAX_SOURCE_LENGTH,
    REASON_CODES,
    AnalysisEvidenceBundle,
    EvidenceStamp,
    EvidenceStatus,
    Freshness,
    GateEvidence,
)


ALL_GATES = {
    "market": True,
    "asset": True,
    "fundamental": True,
    "valuation": True,
    "structure": True,
    "risk": True,
    "execution": True,
}


def _aware_dt(offset_minutes: int = 0) -> datetime:
    base = datetime(2026, 9, 16, 9, 30, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=offset_minutes)


_SENTINEL = object()


def _valid_stamp(
    status: EvidenceStatus = EvidenceStatus.READY,
    freshness: Freshness = Freshness.RECENT,
    reason_code: str | None = None,
    source: str = "akshare",
    as_of: object = _SENTINEL,
    fetched_at: object = _SENTINEL,
    cutoff: object = _SENTINEL,
    market_date: object = _SENTINEL,
) -> EvidenceStamp:
    now = _aware_dt(0)
    resolved_as_of = now - timedelta(minutes=5) if as_of is _SENTINEL else as_of
    resolved_fetched_at = now if fetched_at is _SENTINEL else fetched_at
    resolved_cutoff = now if cutoff is _SENTINEL else cutoff
    resolved_market_date = date(2026, 9, 16) if market_date is _SENTINEL else market_date
    return EvidenceStamp(
        source=source,
        as_of=resolved_as_of,  # type: ignore[arg-type]
        fetched_at=resolved_fetched_at,  # type: ignore[arg-type]
        cutoff=resolved_cutoff,  # type: ignore[arg-type]
        market_date=resolved_market_date,  # type: ignore[arg-type]
        status=status,
        freshness=freshness,
        reason_code=reason_code,
    )


class EvidenceStatusTests(unittest.TestCase):
    def test_status_enum_values_and_str_inheritance(self):
        self.assertEqual(EvidenceStatus.READY, "READY")
        self.assertEqual(EvidenceStatus.DEGRADED, "DEGRADED")
        self.assertEqual(EvidenceStatus.NOT_READY, "NOT_READY")
        self.assertEqual(EvidenceStatus.FAILED, "FAILED")
        self.assertTrue(isinstance(EvidenceStatus.READY, str))
        self.assertEqual(
            [e.value for e in EvidenceStatus],
            ["READY", "DEGRADED", "NOT_READY", "FAILED"],
        )


class FreshnessTests(unittest.TestCase):
    def test_freshness_enum_values_and_str_inheritance(self):
        self.assertEqual(Freshness.RECENT, "RECENT")
        self.assertEqual(Freshness.DELAYED, "DELAYED")
        self.assertEqual(Freshness.STALE, "STALE")
        self.assertEqual(Freshness.FUTURE, "FUTURE")
        self.assertEqual(Freshness.UNKNOWN, "UNKNOWN")
        self.assertTrue(isinstance(Freshness.RECENT, str))
        self.assertEqual(
            [f.value for f in Freshness],
            ["RECENT", "DELAYED", "STALE", "FUTURE", "UNKNOWN"],
        )


class ReasonCodesTests(unittest.TestCase):
    def test_reason_codes_public_and_immutable(self):
        self.assertIsInstance(REASON_CODES, tuple)
        self.assertIn("DATA_MISSING", REASON_CODES)
        self.assertIn("DATA_STALE", REASON_CODES)
        self.assertIn("DATA_FUTURE", REASON_CODES)
        self.assertIn("CUTOFF_EXCEEDED", REASON_CODES)
        self.assertIn("SOURCE_UNAVAILABLE", REASON_CODES)
        self.assertIn("INCOMPLETE_DATA", REASON_CODES)
        self.assertIn("UNCONFIRMED_STRUCTURE", REASON_CODES)


class EvidenceStampValidationTests(unittest.TestCase):
    def test_valid_ready_stamp_construction(self):
        stamp = _valid_stamp()
        self.assertEqual(stamp.source, "akshare")
        self.assertEqual(stamp.status, EvidenceStatus.READY)
        self.assertEqual(stamp.freshness, Freshness.RECENT)
        self.assertIsNone(stamp.reason_code)

    def test_valid_degraded_stamp_construction(self):
        stamp = _valid_stamp(
            status=EvidenceStatus.DEGRADED,
            freshness=Freshness.DELAYED,
            reason_code="DATA_STALE",
        )
        self.assertEqual(stamp.status, EvidenceStatus.DEGRADED)
        self.assertEqual(stamp.freshness, Freshness.DELAYED)
        self.assertEqual(stamp.reason_code, "DATA_STALE")

    def test_rejects_empty_blank_or_non_str_source(self):
        for invalid_source in ("", "   ", None, 123):
            with self.subTest(source=invalid_source):
                with self.assertRaises((ValueError, TypeError)):
                    _valid_stamp(source=invalid_source)  # type: ignore[arg-type]

    def test_rejects_excessive_source_length(self):
        long_source = "s" * (MAX_SOURCE_LENGTH + 1)
        with self.assertRaises(ValueError):
            _valid_stamp(source=long_source)

    def test_rejects_naive_datetimes(self):
        naive_dt = datetime(2026, 9, 16, 9, 30, 0)
        with self.assertRaises(ValueError):
            _valid_stamp(as_of=naive_dt)
        with self.assertRaises(ValueError):
            _valid_stamp(fetched_at=naive_dt)
        with self.assertRaises(ValueError):
            _valid_stamp(cutoff=naive_dt)

    def test_rejects_non_datetime_for_timestamp_fields(self):
        for field in ("as_of", "fetched_at", "cutoff"):
            with self.subTest(field=field):
                kwargs = {field: "2026-09-16T09:30:00Z"}
                with self.assertRaises(TypeError):
                    _valid_stamp(**kwargs)

    def test_rejects_non_date_market_date(self):
        with self.assertRaises(TypeError):
            _valid_stamp(market_date="2026-09-16")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _valid_stamp(market_date=datetime(2026, 9, 16, 9, 30, tzinfo=timezone.utc))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _valid_stamp(market_date=None)  # type: ignore[arg-type]

    def test_rejects_string_impersonating_enum(self):
        with self.assertRaises(TypeError):
            _valid_stamp(status="READY")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _valid_stamp(freshness="RECENT")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _valid_stamp(status="DEGRADED", reason_code="DATA_STALE")  # type: ignore[arg-type]

    def test_rejects_as_of_later_than_cutoff(self):
        t0 = _aware_dt(0)
        with self.assertRaises(ValueError):
            _valid_stamp(as_of=t0 + timedelta(seconds=1), cutoff=t0)

    def test_rejects_as_of_later_than_fetched_at(self):
        t0 = _aware_dt(0)
        with self.assertRaises(ValueError):
            _valid_stamp(as_of=t0 + timedelta(seconds=1), fetched_at=t0)

    def test_rejects_ready_with_reason_code(self):
        with self.assertRaises(ValueError):
            _valid_stamp(status=EvidenceStatus.READY, reason_code="DATA_MISSING")

    def test_rejects_non_ready_without_reason_code(self):
        for status in (EvidenceStatus.DEGRADED, EvidenceStatus.NOT_READY, EvidenceStatus.FAILED):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    _valid_stamp(status=status, reason_code=None)

    def test_rejects_invalid_reason_code(self):
        with self.assertRaises(ValueError):
            _valid_stamp(status=EvidenceStatus.DEGRADED, reason_code="UNKNOWN_RANDOM_CODE")

    def test_rejects_ready_with_future_stale_or_unknown_freshness(self):
        for freshness in (Freshness.FUTURE, Freshness.STALE, Freshness.UNKNOWN):
            with self.subTest(freshness=freshness):
                with self.assertRaises(ValueError):
                    _valid_stamp(status=EvidenceStatus.READY, freshness=freshness)

    def test_evidence_stamp_is_frozen_immutable(self):
        stamp = _valid_stamp()
        with self.assertRaises(FrozenInstanceError):
            stamp.source = "other"  # type: ignore[misc]


class GateEvidenceTests(unittest.TestCase):
    def test_complete_data_does_not_pass_when_domain_criterion_fails(self):
        gate = GateEvidence(
            "fundamental", _valid_stamp(), complete=True, criterion_passed=False,
        )
        self.assertFalse(gate.passed)

    def test_requires_strict_bool_for_complete(self):
        ready_stamp = _valid_stamp()
        for invalid_bool in ("True", "False", 1, 0, None, [], {}):
            with self.subTest(complete=invalid_bool):
                with self.assertRaises(TypeError):
                    GateEvidence(name="industry", stamp=ready_stamp, complete=invalid_bool,
                                 criterion_passed=True)  # type: ignore[arg-type]
        for invalid_bool in ("True", "False", 1, 0, None, [], {}):
            with self.subTest(criterion_passed=invalid_bool):
                with self.assertRaises(TypeError):
                    GateEvidence(name="industry", stamp=ready_stamp, complete=True,
                                 criterion_passed=invalid_bool)  # type: ignore[arg-type]

    def test_rejects_invalid_name_or_stamp(self):
        ready_stamp = _valid_stamp()
        with self.assertRaises(ValueError):
            GateEvidence(name="", stamp=ready_stamp, complete=True, criterion_passed=True)
        with self.assertRaises(TypeError):
            GateEvidence(name="industry", stamp="not_a_stamp", complete=True,
                         criterion_passed=True)  # type: ignore[arg-type]

    def test_passed_only_when_ready_complete_recent_or_delayed_and_no_reason(self):
        recent_stamp = _valid_stamp(status=EvidenceStatus.READY, freshness=Freshness.RECENT)
        delayed_stamp = _valid_stamp(status=EvidenceStatus.READY, freshness=Freshness.DELAYED)
        degraded_stamp = _valid_stamp(
            status=EvidenceStatus.DEGRADED,
            freshness=Freshness.RECENT,
            reason_code="DATA_STALE",
        )
        not_ready_stamp = _valid_stamp(
            status=EvidenceStatus.NOT_READY,
            freshness=Freshness.RECENT,
            reason_code="DATA_MISSING",
        )

        self.assertTrue(GateEvidence("industry", recent_stamp, True, True).passed)
        self.assertTrue(GateEvidence("industry", delayed_stamp, True, True).passed)
        self.assertFalse(GateEvidence("industry", recent_stamp, False, True).passed)
        self.assertFalse(GateEvidence("industry", degraded_stamp, True, True).passed)
        self.assertFalse(GateEvidence("industry", not_ready_stamp, True, True).passed)

    def test_gate_evidence_is_frozen_immutable(self):
        gate = GateEvidence("industry", _valid_stamp(), True, True)
        with self.assertRaises(FrozenInstanceError):
            gate.complete = False  # type: ignore[misc]


class AnalysisEvidenceBundleTests(unittest.TestCase):
    def _make_gate(self, name: str, passed: bool, reason_code: str | None = None) -> GateEvidence:
        if passed:
            stamp = _valid_stamp(status=EvidenceStatus.READY, freshness=Freshness.RECENT)
            return GateEvidence(name=name, stamp=stamp, complete=True,
                                criterion_passed=True)
        code = reason_code or "DATA_MISSING"
        stamp = _valid_stamp(
            status=EvidenceStatus.NOT_READY,
            freshness=Freshness.RECENT,
            reason_code=code,
        )
        return GateEvidence(name=name, stamp=stamp, complete=False,
                            criterion_passed=False)

    def test_ready_only_when_all_four_passed(self):
        all_passed = AnalysisEvidenceBundle(
            industry=self._make_gate("industry", True),
            fundamental=self._make_gate("fundamental", True),
            valuation=self._make_gate("valuation", True),
            structure=self._make_gate("structure", True),
        )
        self.assertTrue(all_passed.ready)
        self.assertEqual(all_passed.blocked_by, ())

    def test_blocked_by_returns_stable_codes_when_not_ready(self):
        partial = AnalysisEvidenceBundle(
            industry=self._make_gate("industry", False, "INDUSTRY_DATA_MISSING"),
            fundamental=self._make_gate("fundamental", True),
            valuation=self._make_gate("valuation", False, "DATA_STALE"),
            structure=self._make_gate("structure", True),
        )
        self.assertFalse(partial.ready)
        self.assertEqual(partial.blocked_by, ("INDUSTRY_DATA_MISSING", "DATA_STALE"))

    def test_bundle_blocked_by_handles_incomplete_stamp_without_reason(self):
        ready_stamp = _valid_stamp(status=EvidenceStatus.READY, freshness=Freshness.RECENT)
        incomplete_gate = GateEvidence(name="structure", stamp=ready_stamp, complete=False,
                                       criterion_passed=True)
        bundle = AnalysisEvidenceBundle(
            industry=self._make_gate("industry", True),
            fundamental=self._make_gate("fundamental", True),
            valuation=self._make_gate("valuation", True),
            structure=incomplete_gate,
        )
        self.assertFalse(bundle.ready)
        self.assertEqual(bundle.blocked_by, ("STRUCTURE_INCOMPLETE",))

    def test_bundle_is_frozen_immutable(self):
        bundle = AnalysisEvidenceBundle(
            industry=self._make_gate("industry", True),
            fundamental=self._make_gate("fundamental", True),
            valuation=self._make_gate("valuation", True),
            structure=self._make_gate("structure", True),
        )
        with self.assertRaises(FrozenInstanceError):
            bundle.industry = self._make_gate("industry", False)  # type: ignore[misc]

    def test_bundle_rejects_non_gate_evidence(self):
        valid = self._make_gate("ok", True)
        with self.assertRaises(TypeError):
            AnalysisEvidenceBundle(industry="invalid", fundamental=valid, valuation=valid, structure=valid)  # type: ignore[arg-type]

    def test_bundle_rejects_gate_wired_to_the_wrong_evidence_slot(self):
        with self.assertRaises(ValueError):
            AnalysisEvidenceBundle(
                industry=self._make_gate("fundamental", True),
                fundamental=self._make_gate("fundamental", True),
                valuation=self._make_gate("valuation", True),
                structure=self._make_gate("structure", True),
            )


class EvaluateGatesInteropTests(unittest.TestCase):
    def test_gate_evidence_passed_read_by_evaluate_gates(self):
        ready_stamp = _valid_stamp(status=EvidenceStatus.READY, freshness=Freshness.RECENT)
        passed_gate = GateEvidence(name="fundamental", stamp=ready_stamp, complete=True,
                                   criterion_passed=True)

        gates = {**ALL_GATES, "fundamental": passed_gate}
        result = evaluate_gates(gates)
        self.assertTrue(result.passed)
        self.assertEqual(result.blocked_by, ())

    def test_gate_evidence_failed_blocks_evaluate_gates(self):
        failed_stamp = _valid_stamp(
            status=EvidenceStatus.DEGRADED,
            freshness=Freshness.RECENT,
            reason_code="DATA_STALE",
        )
        failed_gate = GateEvidence(name="fundamental", stamp=failed_stamp, complete=True,
                                   criterion_passed=True)

        gates = {**ALL_GATES, "fundamental": failed_gate}
        result = evaluate_gates(gates)
        self.assertFalse(result.passed)
        self.assertEqual(result.blocked_by, ("GATE_3_FUNDAMENTAL",))

    def test_truthy_strings_and_exception_objects_cannot_pass(self):
        gates_with_str = {**ALL_GATES, "fundamental": "passed"}
        result_str = evaluate_gates(gates_with_str)
        self.assertFalse(result_str.passed)
        self.assertEqual(result_str.blocked_by, ("GATE_3_FUNDAMENTAL",))

        class TruthyPassedObject:
            passed = "true"

        result_fake = evaluate_gates({**ALL_GATES, "fundamental": TruthyPassedObject()})
        self.assertFalse(result_fake.passed)
        self.assertEqual(result_fake.blocked_by, ("GATE_3_FUNDAMENTAL",))

        class CrashingGate:
            @property
            def passed(self):
                raise RuntimeError("simulated gate evaluation exception")

        result_crash = evaluate_gates({**ALL_GATES, "fundamental": CrashingGate()})
        self.assertFalse(result_crash.passed)
        self.assertEqual(result_crash.blocked_by, ("GATE_3_FUNDAMENTAL",))


if __name__ == "__main__":
    unittest.main()
