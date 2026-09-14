import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from app.portfolio.calculator import build_portfolio_snapshot
from app.portfolio.models import AccountConfig, HoldingConfig, PortfolioConfig
from app.portfolio.risk import apply_portfolio_risk


class PortfolioRiskTests(unittest.TestCase):
    @staticmethod
    def _portfolio_config():
        # Deliberately fabricated cross-account exposures; no production config.
        def holding(code, market, sector, value):
            return HoldingConfig(
                code=code, name="合成持仓 " + code, market=market,
                instrument_type="stock", valuation_mode="exchange",
                cost_price=Decimal("1"), baseline_value=Decimal(value),
                sector=sector, theme="synthetic",
            )
        return PortfolioConfig(2, (
            AccountConfig("DEMO_A", "合成账户一", "long_term_core", date(2026, 1, 5),
                          Decimal("1000"), Decimal("500"), (
                              holding("SYN_CN_1", "CN", "information_technology", "300"),
                              holding("SYN_GLOBAL", "GLOBAL", "healthcare", "200"),
                          )),
            AccountConfig("DEMO_B", "合成账户二", "active_equity", date(2026, 1, 5),
                          Decimal("2000"), Decimal("1040"), (
                              holding("SYN_HK", "HK", "information_technology", "400"),
                              holding("SYN_CN_2", "CN", "healthcare", "560"),
                          )),
        ))

    @staticmethod
    def _snapshot(strategy, holding_ratio):
        total_assets = Decimal("10000")
        holding_value = total_assets * holding_ratio
        holding = HoldingConfig(
            code="TEST",
            name="测试持仓",
            market="CN",
            instrument_type="stock",
            valuation_mode="exchange",
            cost_price=Decimal("1"),
            baseline_value=holding_value,
            baseline_price=Decimal("1"),
            sector="test",
            theme="test",
        )
        account = AccountConfig(
            account_id="TEST",
            name="测试账户",
            strategy=strategy,
            baseline_date=date(2026, 9, 11),
            total_assets=total_assets,
            cash=total_assets - holding_value,
            holdings=(holding,),
        )
        return build_portfolio_snapshot(
            PortfolioConfig(schema_version=2, accounts=(account,)), {}
        )

    @staticmethod
    def _warnings(strategy, holding_value, strategy_config=None):
        snapshot = apply_portfolio_risk(
            PortfolioRiskTests._snapshot(strategy, holding_value), strategy_config
        )
        return snapshot.accounts[0].holdings[0].warnings

    def test_warns_when_active_holding_exceeds_25_percent(self):
        snapshot = apply_portfolio_risk(build_portfolio_snapshot(self._portfolio_config(), {}))
        holding = snapshot.accounts[1].holdings[1]
        self.assertIn("集中度预警", holding.warnings)

    def test_aggregates_sector_exposure_across_accounts(self):
        snapshot = apply_portfolio_risk(build_portfolio_snapshot(self._portfolio_config(), {}))
        self.assertEqual(snapshot.exposure_by_sector["healthcare"], Decimal("0.2533"))
        self.assertEqual(snapshot.exposure_by_sector["information_technology"], Decimal("0.2333"))

    def test_aggregates_exact_market_exposure_across_accounts(self):
        snapshot = apply_portfolio_risk(build_portfolio_snapshot(self._portfolio_config(), {}))
        self.assertEqual(snapshot.exposure_by_market["CN"], Decimal("0.2867"))
        self.assertEqual(snapshot.exposure_by_market["HK"], Decimal("0.1333"))
        self.assertEqual(snapshot.exposure_by_market["GLOBAL"], Decimal("0.0667"))

    def test_uses_actual_strategy_config_with_synthetic_accounts(self):
        strategy_path = Path(__file__).parents[1] / "config" / "strategy.yaml"
        with strategy_path.open(encoding="utf-8") as handle:
            strategy_config = yaml.safe_load(handle)
        snapshot = apply_portfolio_risk(
            build_portfolio_snapshot(self._portfolio_config(), {}), strategy_config
        )
        self.assertIn("集中度预警", snapshot.accounts[1].holdings[1].warnings)

    def test_long_term_core_boundaries(self):
        self.assertIn("集中度预警", self._warnings("long_term_core", Decimal("0.30")))
        self.assertNotIn("高集中度风险", self._warnings("long_term_core", Decimal("0.30")))
        self.assertIn("高集中度风险", self._warnings("long_term_core", Decimal("0.35")))

    def test_active_equity_boundaries(self):
        self.assertNotIn("集中度预警", self._warnings("active_equity", Decimal("0.25")))
        self.assertIn("集中度预警", self._warnings("active_equity", Decimal("0.2501")))
        self.assertIn("高集中度风险", self._warnings("active_equity", Decimal("0.30")))

    def test_rejects_invalid_threshold_pairs(self):
        invalid_configs = (
            {"concentration_warning": "0.40", "concentration_high": "0.30"},
            {"concentration_warning": "-0.01", "concentration_high": "0.30"},
            {"concentration_warning": "0.30", "concentration_high": "1.01"},
        )
        for values in invalid_configs:
            with self.subTest(values=values), self.assertRaises(ValueError):
                apply_portfolio_risk(
                    self._snapshot("active_equity", Decimal("0.20")),
                    {"accounts": {"active_equity": values}},
                )

    def test_rejects_missing_threshold_members(self):
        for values in (
            {"concentration_warning": "0.25"},
            {"concentration_high": "0.30"},
            {},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                apply_portfolio_risk(
                    self._snapshot("active_equity", Decimal("0.20")),
                    {"accounts": {"active_equity": values}},
                )

    def test_keeps_default_thresholds_without_config(self):
        self.assertIn("集中度预警", self._warnings("active_equity", Decimal("0.26")))

    def test_does_not_mutate_original_snapshot(self):
        original = build_portfolio_snapshot(self._portfolio_config(), {})
        enriched = apply_portfolio_risk(original)
        self.assertIsNot(enriched, original)
        self.assertEqual(original.exposure_by_sector, {})
        self.assertEqual(original.exposure_by_market, {})
        self.assertEqual(original.accounts[1].holdings[1].warnings, ())
        self.assertNotEqual(enriched.accounts, original.accounts)

    def test_combined_cash_is_not_treated_as_account_cash(self):
        snapshot = apply_portfolio_risk(build_portfolio_snapshot(self._portfolio_config(), {}))
        self.assertEqual(snapshot.cash, Decimal("1540.00"))
        self.assertEqual(snapshot.position_percent, Decimal("0.4867"))
        self.assertEqual(snapshot.accounts[0].cash, Decimal("500.00"))
        self.assertEqual(snapshot.accounts[1].cash, Decimal("1040.00"))
