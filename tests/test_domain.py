import unittest

from app.chan.multi_cycle import CYCLES, cycle_role
from app.chan.signal import ChanSignal
from app.config.loader import load_yaml
from app.domain import (
    TIMEFRAME_ROLES,
    BarStatus,
    CycleRole,
    DecisionAction,
    SignalType,
    Timeframe,
)


class EnumContractTests(unittest.TestCase):
    def test_timeframe_members_complete(self):
        self.assertEqual(
            {tf.name for tf in Timeframe},
            {"WEEKLY", "DAILY", "MIN_120", "MIN_30", "MIN_15", "MIN_5"},
        )

    def test_bar_status_members_complete(self):
        self.assertEqual(
            {status.name for status in BarStatus},
            {"CLOSED", "FORMING", "MISSING", "STALE", "INVALID"},
        )

    def test_signal_type_members_complete(self):
        self.assertEqual(
            {signal.name for signal in SignalType},
            {"NONE", "FIRST_BUY", "CLASS_SECOND_BUY", "SECOND_BUY", "THIRD_BUY", "SELL_RISK"},
        )

    def test_decision_action_members_complete(self):
        self.assertEqual(
            {action.name for action in DecisionAction},
            {"BUY", "ADD", "HOLD", "WAIT", "REDUCE", "EXIT"},
        )


class TimeframeRoleTests(unittest.TestCase):
    def test_timeframe_to_cycle_role_mapping(self):
        expected = {
            Timeframe.WEEKLY: CycleRole.WEEKLY_POSITION,
            Timeframe.DAILY: CycleRole.DAILY_DIRECTION,
            Timeframe.MIN_120: CycleRole.CORE_BUY_POINT,
            Timeframe.MIN_30: CycleRole.PULLBACK_CONFIRMATION,
            Timeframe.MIN_15: CycleRole.EXECUTION,
            Timeframe.MIN_5: CycleRole.PRICE_OPTIMIZATION,
        }
        self.assertEqual(TIMEFRAME_ROLES, expected)

    def test_multi_cycle_table_matches_domain_roles(self):
        expected = {
            "weekly": "position",
            "daily": "direction",
            "120m": "core_buy_point",
            "30m": "pullback_confirmation",
            "15m": "execution",
            "5m": "price_optimization",
        }
        self.assertEqual(CYCLES, expected)
        self.assertEqual(cycle_role("5m"), "price_optimization")
        self.assertEqual(cycle_role("15m"), "execution")
        self.assertEqual(cycle_role("unknown-cycle"), "unknown")


class SignalCompatTests(unittest.TestCase):
    def test_signal_type_values_match_chan_signal(self):
        self.assertEqual(
            {s.value for s in SignalType}, {s.value for s in ChanSignal}
        )
        self.assertEqual(SignalType.NONE.value, ChanSignal.WAIT.value)


class RulesConfigTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_yaml("rules.yaml")

    def test_version_and_macd(self):
        self.assertEqual(self.rules["version"], "8.1")
        self.assertEqual(
            self.rules["technical"]["macd"],
            {"fast": 6, "slow": 13, "signal": 4},
        )

    def test_risk_fields(self):
        risk = self.rules["risk"]
        self.assertEqual(risk["trade_risk_min"], 0.005)
        self.assertEqual(risk["trade_risk_max"], 0.010)
        self.assertEqual(risk["minimum_rr"], 2.0)
        self.assertEqual(risk["preferred_rr"], 2.5)

    def test_cycles_responsibilities(self):
        self.assertEqual(
            self.rules["cycles"],
            {
                "weekly": "position",
                "daily": "direction",
                "120m": "core_buy_point",
                "30m": "pullback_confirmation",
                "15m": "execution",
                "5m": "price_optimization",
            },
        )

    def test_decision_gates_and_auto_execute(self):
        self.assertTrue(self.rules["decision"]["require_all_seven_gates"])
        self.assertFalse(self.rules["decision"]["auto_execute"])


if __name__ == "__main__":
    unittest.main()
