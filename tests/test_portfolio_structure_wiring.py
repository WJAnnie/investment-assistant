import json
import re
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.portfolio.analysis import analyze_portfolio
from app.portfolio.calculator import build_portfolio_snapshot
from app.portfolio.models import AccountConfig, HoldingConfig, PortfolioConfig
from app.utils.serialization import to_jsonable

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW_EVENING = datetime(2026, 9, 11, 20, 0, tzinfo=SHANGHAI)


def _config(code="600000", market="CN"):
    holding = HoldingConfig(
        code=code,
        name="浦发银行",
        market=market,
        instrument_type="stock",
        valuation_mode="exchange",
        cost_price=Decimal("9"),
        baseline_value=Decimal("1000"),
        sector="financials",
        theme="bank",
        quantity=Decimal("100"),
    )
    account = AccountConfig(
        account_id="A",
        name="A账户",
        strategy="long_term_core",
        baseline_date=date(2026, 9, 11),
        total_assets=Decimal("1500"),
        cash=Decimal("500"),
        holdings=(holding,),
    )
    return PortfolioConfig(schema_version=2, accounts=(account,))


def _daily_lines(count=35, end_date=date(2026, 9, 11)):
    return [
        KLine(
            (end_date - timedelta(days=count - 1 - index)).isoformat(),
            100 + index,
            101 + index,
            99 + index,
            100 + index,
            1000,
        )
        for index in range(count)
    ]


def _weekly_lines(count=35, end_date=date(2026, 9, 4)):
    return [
        KLine(
            (end_date - timedelta(weeks=count - 1 - index)).isoformat(),
            100 + index,
            101 + index,
            99 + index,
            100 + index,
            1000,
        )
        for index in range(count)
    ]


class PortfolioStructureWiringTests(unittest.TestCase):
    def _analyze(self, lines, *, now=NOW_EVENING, market="CN", period="daily"):
        config = _config(market=market)
        snapshot = build_portfolio_snapshot(config, {})
        return analyze_portfolio(
            config,
            snapshot,
            history_loader=lambda _target, **_kwargs: list(lines),
            now=now,
            history_period=period,
            min_history_bars=30,
        )

    def test_fresh_daily_lines_produces_wait_structure_evidence(self):
        lines = _daily_lines(35, end_date=date(2026, 9, 11))
        result = self._analyze(lines, now=NOW_EVENING)
        item = result["items"][0]

        self.assertEqual(item["status"], "ready")
        self.assertIn("structure", item)
        self.assertIsNotNone(item["structure"])

        structure = item["structure"]
        self.assertIs(structure["ready"], False)
        self.assertEqual(structure["outcome"], "WAIT")
        self.assertEqual(structure["reason_code"], "STRUCTURE_DATA_MISSING")
        self.assertEqual(len(structure["per_cycle_status"]), 6)
        self.assertEqual(structure["per_cycle_status"]["daily"], "closed")

        missing = set(structure["missing_cycles"])
        for cycle in ("weekly", "120m", "30m", "15m"):
            self.assertIn(cycle, missing)

    def test_fail_closed_invariant_never_triggers_buy_signal(self):
        buy_signals = {"FIRST_BUY", "CLASS_SECOND_BUY", "SECOND_BUY", "THIRD_BUY"}

        lines_up = _daily_lines(35, end_date=date(2026, 9, 11))
        lines_weekly = _weekly_lines(35, end_date=date(2026, 9, 4))
        lines_down = [
            KLine(
                (date(2026, 9, 11) - timedelta(days=34 - i)).isoformat(),
                200 - i,
                201 - i,
                199 - i,
                200 - i,
                1000,
            )
            for i in range(35)
        ]
        lines_rebound = [
            KLine(
                (date(2026, 9, 11) - timedelta(days=34 - i)).isoformat(),
                150 - i if i < 25 else 125 + (i - 25) * 3,
                151 - i if i < 25 else 126 + (i - 25) * 3,
                149 - i if i < 25 else 124 + (i - 25) * 3,
                150 - i if i < 25 else 125 + (i - 25) * 3,
                1000,
            )
            for i in range(35)
        ]

        cases = [
            ("daily_up", lines_up, "daily"),
            ("weekly", lines_weekly, "weekly"),
            ("daily_down", lines_down, "daily"),
            ("daily_rebound", lines_rebound, "daily"),
        ]

        for label, lines, period in cases:
            with self.subTest(case=label):
                result = self._analyze(lines, now=NOW_EVENING, period=period)
                item = result["items"][0]
                self.assertEqual(item["status"], "ready")
                self.assertIsNotNone(item.get("structure"))
                self.assertIs(item["structure"]["ready"], False)
                self.assertNotIn(item["signal"], buy_signals)
                self.assertIn(item["signal"], {"WAIT", "SELL_RISK"})

    def test_stale_daily_lines_downgraded_to_stale(self):
        stale_end_date = date(2026, 9, 1)
        lines = _daily_lines(35, end_date=stale_end_date)
        result = self._analyze(lines, now=NOW_EVENING)
        item = result["items"][0]

        self.assertEqual(item["status"], "ready")
        self.assertIsNotNone(item.get("structure"))
        structure = item["structure"]
        self.assertEqual(structure["per_cycle_status"]["daily"], "stale")
        self.assertIn("daily", structure["stale_cycles"])

    def test_weekly_period_populates_weekly_closed_and_daily_missing(self):
        lines = _weekly_lines(35, end_date=date(2026, 9, 4))
        # Tuesday following the closed Friday bar (lag = 4 days <= 7 days)
        now_midweek = datetime(2026, 9, 8, 15, 0, tzinfo=SHANGHAI)
        result = self._analyze(lines, now=now_midweek, period="weekly")
        item = result["items"][0]

        self.assertEqual(item["status"], "ready")
        self.assertIsNotNone(item.get("structure"))
        structure = item["structure"]
        self.assertEqual(structure["per_cycle_status"]["weekly"], "closed")
        self.assertEqual(structure["per_cycle_status"]["daily"], "missing")

    def test_analysis_source_code_has_no_hardcoded_multi_cycle_confirm_true(self):
        source_path = Path(__file__).resolve().parent.parent / "app" / "portfolio" / "analysis.py"
        content = source_path.read_text(encoding="utf-8")
        match = re.search(r'["\']multi_cycle_confirm["\']\s*:\s*True', content)
        self.assertIsNone(
            match,
            "multi_cycle_confirm must never be hardcoded to True in analysis.py",
        )

    def test_data_limits_has_structure_and_preserves_existing_keys(self):
        lines = _daily_lines(35, end_date=date(2026, 9, 11))
        result = self._analyze(lines, now=NOW_EVENING)
        limits = result["data_limits"]

        self.assertEqual(limits.get("structure"), "dated_cycles_only")
        for key in ("fundamental", "industry", "news", "minute"):
            self.assertIn(key, limits)

    def test_structure_payload_is_jsonable(self):
        lines = _daily_lines(35, end_date=date(2026, 9, 11))
        result = self._analyze(lines, now=NOW_EVENING)
        item = result["items"][0]
        self.assertIn("structure", item)
        self.assertIsNotNone(item["structure"])

        jsonable = to_jsonable(item["structure"])
        serialized = json.dumps(jsonable)
        self.assertIsInstance(serialized, str)
        deserialized = json.loads(serialized)
        self.assertEqual(deserialized["outcome"], "WAIT")
        self.assertIs(deserialized["ready"], False)


if __name__ == "__main__":
    unittest.main()
