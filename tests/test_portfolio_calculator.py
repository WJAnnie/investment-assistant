import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.portfolio.calculator import build_portfolio_snapshot
from app.portfolio.loader import load_portfolio
from app.portfolio.models import AccountConfig, HoldingConfig, PortfolioConfig, Valuation


class PortfolioCalculatorTests(unittest.TestCase):
    def setUp(self):
        self.config = load_portfolio(Path(__file__).parents[1] / "config" / "portfolio.example.yaml")
        now = datetime(2026, 1, 5, 8, 39, tzinfo=timezone.utc)
        self.valuations = {
            "600000": Valuation("600000", Decimal("10.000"), Decimal("0"), now, "fixture"),
        }

    def test_reproduces_synthetic_baseline_totals(self):
        result = build_portfolio_snapshot(self.config, self.valuations)
        self.assertEqual(result.total_assets, Decimal("3000.50"))
        self.assertEqual(result.holdings_value, Decimal("800.00"))
        self.assertEqual(result.cash, Decimal("2200.50"))
        self.assertEqual(result.position_percent, Decimal("0.2666"))

    def test_quantity_account_uses_exact_market_value(self):
        result = build_portfolio_snapshot(self.config, self.valuations)
        holding = result.accounts[1].holdings[0]
        self.assertEqual(holding.market_value, Decimal("500.00"))
        self.assertEqual(holding.value_precision, "exact")

    def test_baseline_account_does_not_invent_monetary_profit_without_units(self):
        result = build_portfolio_snapshot(self.config, {})
        holding = result.accounts[0].holdings[0]
        self.assertEqual(holding.market_value, Decimal("300.00"))
        self.assertIsNone(holding.monetary_profit)
        self.assertEqual(holding.value_precision, "baseline")

    def test_failed_live_valuation_keeps_baseline_value(self):
        failed = {"600000": Valuation(
            "600000", Decimal("0"), Decimal("0"),
            datetime(2026, 1, 5, 8, 39, tzinfo=timezone.utc),
            "fixture", "failed", "offline",
        )}
        holding = build_portfolio_snapshot(self.config, failed).accounts[1].holdings[0]
        self.assertEqual(holding.market_value, Decimal("500.00"))
        self.assertEqual(holding.value_precision, "baseline")
        self.assertIsNone(holding.monetary_profit)
        self.assertIsNone(holding.return_from_cost)

    def test_changed_fresh_price_calculates_market_value_and_monetary_profit(self):
        valuations = {"600000": Valuation(
            "600000", Decimal("11.000"), Decimal("0"),
            datetime(2026, 1, 5, 8, 39, tzinfo=timezone.utc), "fixture",
        )}
        holding = build_portfolio_snapshot(self.config, valuations).accounts[1].holdings[0]
        self.assertEqual(holding.market_value, Decimal("550.00"))
        self.assertEqual(holding.monetary_profit, Decimal("56.18"))
        self.assertEqual(holding.value_precision, "exact")

    def test_unusable_valuation_preserves_inconsistent_baseline_value(self):
        holding = HoldingConfig(
            code="TEST", name="Test", market="CN", instrument_type="stock",
            valuation_mode="exchange", cost_price=Decimal("1"),
            baseline_value=Decimal("100.01"), baseline_price=Decimal("1"),
            sector="test", theme="test", quantity=Decimal("600"),
        )
        account = AccountConfig(
            account_id="TEST", name="Test", strategy="test",
            baseline_date=datetime(2026, 9, 11).date(),
            total_assets=Decimal("100.01"), cash=Decimal("0"), holdings=(holding,),
        )
        config = PortfolioConfig(schema_version=2, accounts=(account,))
        failed = Valuation(
            "TEST", Decimal("0"), Decimal("0"),
            datetime(2026, 9, 11, 8, 39, tzinfo=timezone.utc), "fixture",
            "failed", "offline",
        )
        for valuations in ({}, {"TEST": failed}):
            with self.subTest(valuations=valuations):
                result = build_portfolio_snapshot(config, valuations)
                snapshot = result.accounts[0].holdings[0]
                self.assertEqual(snapshot.market_value, Decimal("100.01"))
                self.assertEqual(snapshot.value_precision, "baseline")
                self.assertIsNone(snapshot.monetary_profit)
        self.assertIsNone(build_portfolio_snapshot(config, {}).accounts[0].holdings[0].return_from_cost)

    def test_rejects_schema_version_one(self):
        with self.assertRaisesRegex(ValueError, "schema_version 2"):
            build_portfolio_snapshot(PortfolioConfig(schema_version=1), {})

    def test_rounds_money_account_weight_and_position_ratio_half_up(self):
        holding = HoldingConfig(
            code="ROUND", name="Round", market="CN", instrument_type="stock",
            valuation_mode="exchange", cost_price=Decimal("1"),
            baseline_value=Decimal("1.005"), sector="test", theme="test",
        )
        account = AccountConfig(
            account_id="ROUND", name="Round", strategy="test",
            baseline_date=datetime(2026, 9, 11).date(),
            total_assets=Decimal("2.015"), cash=Decimal("1.01"), holdings=(holding,),
        )
        rounded = build_portfolio_snapshot(PortfolioConfig(schema_version=2, accounts=(account,)), {})
        snapshot = rounded.accounts[0].holdings[0]
        self.assertEqual(snapshot.market_value, Decimal("1.01"))

        boundary_holding = HoldingConfig(
            code="BOUNDARY", name="Boundary", market="CN", instrument_type="stock",
            valuation_mode="exchange", cost_price=Decimal("1"),
            baseline_value=Decimal("2469.00"), sector="test", theme="test",
        )
        boundary_account = AccountConfig(
            account_id="BOUNDARY", name="Boundary", strategy="test",
            baseline_date=datetime(2026, 9, 11).date(),
            total_assets=Decimal("20000.00"), cash=Decimal("17531.00"),
            holdings=(boundary_holding,),
        )
        result = build_portfolio_snapshot(
            PortfolioConfig(schema_version=2, accounts=(boundary_account,)), {}
        )
        boundary = result.accounts[0].holdings[0]
        self.assertEqual(boundary.account_weight, Decimal("0.1235"))
        self.assertEqual(result.accounts[0].position_percent, Decimal("0.1235"))
