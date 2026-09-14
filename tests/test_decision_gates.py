import unittest
from decimal import Decimal

from app.chan.signal import ChanSignal
from app.decision.engine import build_decision
from app.decision.gates import GATE_CODES, evaluate_gates
from app.risk import InvalidationPoint, RiskContext


ALL_GATES = {
    "market": True,
    "asset": True,
    "fundamental": True,
    "valuation": True,
    "structure": True,
    "risk": True,
    "execution": True,
}


def _risk_context():
    return RiskContext(
        account_assets=Decimal("100000"),
        entry=Decimal("10"),
        target=Decimal("12.4"),
        invalidation=InvalidationPoint(Decimal("9")),
    )


class GateEvaluationTests(unittest.TestCase):
    def test_all_seven_gates_must_be_explicitly_true(self):
        self.assertTrue(evaluate_gates(ALL_GATES).passed)
        missing = evaluate_gates(None)
        self.assertFalse(missing.passed)
        self.assertEqual(missing.blocked_by, tuple(GATE_CODES.values()))

    def test_each_failed_gate_has_a_stable_block_code(self):
        for name, code in GATE_CODES.items():
            with self.subTest(gate=name):
                result = evaluate_gates({**ALL_GATES, name: False})
                self.assertFalse(result.passed)
                self.assertEqual(result.blocked_by, (code,))

    def test_truthy_strings_and_unknown_values_do_not_pass(self):
        result = evaluate_gates({**ALL_GATES, "market": "PASS"})
        self.assertFalse(result.passed)
        self.assertEqual(result.blocked_by, ("GATE_1_MARKET",))

    def test_gate_adapter_exceptions_fail_closed(self):
        class BrokenGates(dict):
            def get(self, _name, _default=None):
                raise RuntimeError("adapter failed")

        result = evaluate_gates(BrokenGates(ALL_GATES))
        self.assertFalse(result.passed)
        self.assertEqual(result.blocked_by, tuple(GATE_CODES.values()))


class DecisionGateTests(unittest.TestCase):
    def test_missing_gates_cannot_be_compensated_by_score_and_rr(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=99,
            risk_context=_risk_context(),
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertIn("GATE_1_MARKET", decision.blocked_by)
        self.assertIn("GATE_7_EXECUTION", decision.blocked_by)

    def test_one_failed_gate_blocks_buy(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=99,
            risk_context=_risk_context(),
            gates={**ALL_GATES, "valuation": False},
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.blocked_by, ("GATE_4_VALUATION",))

    def test_all_gates_and_internal_risk_checks_allow_buy(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            risk_context=_risk_context(),
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "BUY")
        self.assertEqual(decision.blocked_by, ())

    def test_sell_risk_precedes_missing_technical_score(self):
        flat = build_decision(
            ChanSignal.SELL_RISK,
            technical_score=None,
            current_position=0,
        )
        held = build_decision(
            ChanSignal.SELL_RISK,
            technical_score=None,
            current_position=Decimal("0.2"),
        )
        self.assertEqual(flat.action, "WAIT")
        self.assertEqual(held.action, "REDUCE")
        self.assertEqual(held.blocked_by, ("GATE_6_RISK",))

    def test_missing_technical_score_and_evidence_fail_closed(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=None,
            current_position=Decimal("0.1"),
            risk_context=_risk_context(),
            new_evidence=None,
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "HOLD")
        self.assertIn("TECHNICAL_SCORE_MISSING", decision.blocked_by)

    def test_invalid_signal_is_rejected_explicitly(self):
        with self.assertRaises(ValueError):
            build_decision("SECOND_BUY", technical_score=85)

    def test_risk_blocked_requires_a_strict_boolean(self):
        with self.assertRaises(ValueError):
            build_decision(
                ChanSignal.WAIT,
                technical_score=None,
                risk_blocked="false",
            )


if __name__ == "__main__":
    unittest.main()
