import unittest
from decimal import Decimal
from unittest.mock import patch

from app.chan.signal import ChanSignal
from app.decision.engine import build_decision
from app.risk import (
    InvalidationPoint,
    RiskContext,
    calculate_position_size,
    calculate_risk_budget,
    calculate_rr,
    rr_thresholds,
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


def _context(entry=Decimal("10"), target=Decimal("13"), stop=Decimal("9"),
             assets=Decimal("100000")):
    return RiskContext(
        account_assets=assets,
        entry=entry,
        target=target,
        invalidation=InvalidationPoint(stop),
    )


class RRCalculationTests(unittest.TestCase):
    def test_rr_thresholds_load_config_and_validate_order(self):
        self.assertEqual(
            rr_thresholds({"risk": {"minimum_rr": "2.2", "preferred_rr": "2.8"}}),
            (Decimal("2.2"), Decimal("2.8")),
        )
        with self.assertRaises(ValueError):
            rr_thresholds({"risk": {"minimum_rr": "2.5", "preferred_rr": "2.0"}})

    def test_rr_thresholds_reject_non_positive_and_non_numeric_values(self):
        for risk in (
            {"minimum_rr": "not-a-number", "preferred_rr": "2.5"},
            {"minimum_rr": "0", "preferred_rr": "2.5"},
            {"minimum_rr": "2.0", "preferred_rr": "-1"},
        ):
            with self.subTest(risk=risk), self.assertRaises(ValueError):
                rr_thresholds({"risk": risk})

    def test_rr_thresholds_reject_falsey_non_mapping_risk_sections(self):
        for risk in (False, [], ""):
            with self.subTest(risk=risk), self.assertRaises(ValueError):
                rr_thresholds({"risk": risk})

    def test_rr_thresholds_fall_back_when_config_cannot_be_read(self):
        with patch("app.risk.rr.load_risk_rules", side_effect=OSError("denied")):
            self.assertEqual(
                rr_thresholds(), (Decimal("2.0"), Decimal("2.5"))
            )

    def test_decision_uses_configured_rr_thresholds(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            risk_context=_context(
                entry=Decimal("10"), target=Decimal("12.2"), stop=Decimal("9")
            ),
            gates=ALL_GATES,
            rules={"risk": {"minimum_rr": "2.3", "preferred_rr": "2.8"}},
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertIn("GATE_6_RISK_RR", decision.blocked_by)

    def test_preferred_rr_is_explanatory_not_a_hard_gate(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            risk_context=_context(
                entry=Decimal("10"), target=Decimal("12.4"), stop=Decimal("9")
            ),
            gates=ALL_GATES,
            rules={"risk": {"minimum_rr": "2.0", "preferred_rr": "2.5"}},
        )
        self.assertEqual(decision.action, "BUY")
        self.assertTrue(any("优选" in reason for reason in decision.reasons))

    def test_rr_below_minimum_is_rejected_by_decision(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            risk_context=_context(entry=Decimal("10"), target=Decimal("11.9"),
                                   stop=Decimal("9")),
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertIn("GATE_6_RISK_RR", decision.blocked_by)

    def test_rr_at_boundary_2_0_passes(self):
        self.assertEqual(calculate_rr("10", "12", "9"), Decimal("2"))
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            risk_context=_context(entry=Decimal("10"), target=Decimal("12"),
                                   stop=Decimal("9")),
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "BUY")
        # 风险预算 500（100000×0.5%），每股风险 1 → 500 股 × 10 = 5% 仓位
        self.assertEqual(decision.position_percent, 0.05)

    def test_rr_2_5_is_preferred_zone(self):
        self.assertEqual(calculate_rr("10", "12.5", "9"), Decimal("2.5"))

    def test_non_positive_prices_are_rejected(self):
        with self.assertRaises(ValueError):
            calculate_rr("-10", "-9", "-11")  # 全负价格几何上“合法”
        with self.assertRaises(ValueError):
            calculate_rr("0", "12", "-1")
        with self.assertRaises(ValueError):
            calculate_rr("10", "12", "0")

    def test_invalid_dict_risk_context_degrades_to_wait(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            risk_context={"account_assets": "100000", "entry": "10",
                          "target": "12", "invalidation": "-9"},  # 非法失效点
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertIn("GATE_6_RISK_INPUT", decision.blocked_by)

    def test_malformed_dict_risk_context_degrades_to_wait(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            risk_context={"account_assets": "100000"},  # 缺失必需键
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertIn("GATE_6_RISK_INPUT", decision.blocked_by)

    def test_zero_or_negative_per_share_risk_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_rr("10", "12", "10")  # 风险为 0
        with self.assertRaises(ValueError):
            calculate_rr("10", "12", "11")  # 失效点在入场价上方，风险为负


class PositionSizeTests(unittest.TestCase):
    def test_position_is_capped_by_tightest_limit(self):
        size = calculate_position_size(
            risk_amount=Decimal("1000"),
            entry=Decimal("10"),
            invalidation=InvalidationPoint(Decimal("9")),
            account_assets=Decimal("100000"),
            position_caps={
                "max_position": Decimal("0.30"),
                "exchange_limit": Decimal("0.02"),
            },
        )
        self.assertEqual(size.theoretical_shares, Decimal("1000"))  # 1000/1
        self.assertEqual(size.position_percent, Decimal("0.02"))
        self.assertIn("exchange_limit", size.capped_by)

    def test_zero_or_negative_per_share_risk_rejected(self):
        with self.assertRaises(ValueError):
            calculate_position_size(
                risk_amount=Decimal("1000"),
                entry=Decimal("10"),
                invalidation=InvalidationPoint(Decimal("10")),
                account_assets=Decimal("100000"),
            )
        with self.assertRaises(ValueError):
            calculate_position_size(
                risk_amount=Decimal("1000"),
                entry=Decimal("10"),
                invalidation=InvalidationPoint(Decimal("11")),
                account_assets=Decimal("100000"),
            )


class DecisionEvidenceTests(unittest.TestCase):
    def test_add_without_new_evidence_is_downgraded_to_hold(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            current_position=0.10,
            risk_context=_context(),
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "HOLD")
        self.assertIn("EVIDENCE_REQUIRED_FOR_ADD", decision.blocked_by)
        self.assertEqual(decision.position_percent, 0.0)

    def test_add_with_new_evidence_passes(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            current_position=0.10,
            risk_context=_context(),
            new_evidence=("30m 回调确认",),
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "ADD")
        self.assertEqual(decision.new_evidence, ("30m 回调确认",))
        self.assertFalse(decision.auto_execute)

    def test_blank_or_null_evidence_cannot_unlock_add(self):
        for evidence in ("", "   ", [None], ["  "]):
            with self.subTest(evidence=evidence):
                decision = build_decision(
                    ChanSignal.SECOND_BUY,
                    technical_score=85,
                    current_position=0.10,
                    risk_context=_context(),
                    new_evidence=evidence,
                    gates=ALL_GATES,
                )
                self.assertEqual(decision.action, "HOLD")
                self.assertIn("EVIDENCE_REQUIRED_FOR_ADD", decision.blocked_by)

    def test_add_respects_remaining_room_under_max_position(self):
        context = RiskContext(
            account_assets=Decimal("100000"),
            entry=Decimal("10"),
            target=Decimal("12"),
            invalidation=InvalidationPoint(Decimal("9")),
            risk_ratio=Decimal("0.01"),
        )
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            current_position=Decimal("0.25"),
            max_position=Decimal("0.30"),
            risk_context=context,
            new_evidence=("新增确认",),
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "ADD")
        self.assertEqual(decision.position_percent, 0.05)

    def test_missing_risk_context_keeps_research_only_position_zero(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.position_percent, 0.0)
        self.assertTrue(any("未提供风险上下文" in r for r in decision.reasons))
        self.assertIn("GATE_6_RISK_INPUT", decision.blocked_by)


class RiskBudgetTests(unittest.TestCase):
    def test_default_budget_is_half_percent(self):
        budget = calculate_risk_budget(Decimal("100000"))
        self.assertEqual(budget, Decimal("500"))  # 默认下限 0.5%

    def test_explicit_ratio_within_band(self):
        budget = calculate_risk_budget(Decimal("100000"), risk_ratio="0.01")
        self.assertEqual(budget, Decimal("1000"))

    def test_ratio_outside_band_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_risk_budget(Decimal("100000"), risk_ratio="0.02")
        with self.assertRaises(ValueError):
            calculate_risk_budget(Decimal("100000"), risk_ratio="0.001")

    def test_falsey_non_mapping_risk_section_is_rejected(self):
        for risk in (False, [], ""):
            with self.subTest(risk=risk), self.assertRaises(ValueError):
                calculate_risk_budget(Decimal("100000"), rules={"risk": risk})




class SevereFixRegressionTests(unittest.TestCase):
    """Antigravity 审查发现的 S1/S2/M2/M3/M4 回归测试。"""

    def test_s2_zero_position_blocks_buy(self):
        # max_position=0 → 裁剪到 0 → 不得输出 BUY
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            max_position=0.0,
            risk_context=_context(),
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.position_percent, 0.0)
        self.assertIn("GATE_6_RISK_POSITION", decision.blocked_by)

    def test_s2_full_position_blocks_add(self):
        # 满仓 remaining_room=0 → 不得输出 ADD
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            current_position=1.0,
            risk_context=_context(),
            new_evidence=("30m 回调确认",),
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "HOLD")
        self.assertIn("GATE_6_RISK_POSITION", decision.blocked_by)

    def test_m2_invalidation_mapping_form_is_accepted(self):
        ctx = RiskContext(
            account_assets=Decimal("100000"),
            entry=Decimal("10"),
            target=Decimal("12"),
            invalidation={"price": Decimal("9"), "reason": "前低"},
        )
        decision = build_decision(
            ChanSignal.SECOND_BUY, technical_score=85, risk_context=ctx,
            gates=ALL_GATES,
        )
        self.assertEqual(decision.action, "BUY")

    def test_m2_risk_ratio_percent_string_is_accepted(self):
        ctx = RiskContext(
            account_assets=Decimal("100000"),
            entry=Decimal("10"),
            target=Decimal("12"),
            invalidation=InvalidationPoint(Decimal("9")),
            risk_ratio="1%",
        )
        budget = calculate_risk_budget(ctx.account_assets, risk_ratio=ctx.risk_ratio)
        self.assertEqual(budget, Decimal("1000"))

    def test_m3_position_size_has_builtin_hundred_percent_cap(self):
        size = calculate_position_size(
            risk_amount=Decimal("999999"),
            entry=Decimal("10"),
            invalidation=InvalidationPoint(Decimal("9.99")),
            account_assets=Decimal("100000"),
        )
        self.assertLessEqual(size.position_percent, Decimal("1"))

    def test_m4_infinite_technical_score_is_rejected(self):
        for score in (float("inf"), float("nan")):
            with self.subTest(score=score):
                decision = build_decision(
                    ChanSignal.SECOND_BUY,
                    technical_score=score,
                    gates=ALL_GATES,
                )
                self.assertEqual(decision.action, "WAIT")
                self.assertIn("TECHNICAL_SCORE_INVALID", decision.blocked_by)

    def test_g5_string_new_evidence_is_wrapped_not_split(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            current_position=0.10,
            risk_context=_context(),
            new_evidence="30m 回调确认",
            gates=ALL_GATES,
        )
        self.assertEqual(decision.new_evidence, ("30m 回调确认",))


if __name__ == "__main__":
    unittest.main()
