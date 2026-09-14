import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType

from app.portfolio import (
    AccountConfig,
    AccountSnapshot,
    HoldingConfig,
    HoldingSnapshot,
    PortfolioConfig,
    PortfolioSnapshot,
)
from app.portfolio.loader import load_portfolio, parse_portfolio


class PortfolioLoaderTests(unittest.TestCase):
    @staticmethod
    def valid_payload():
        return {
            "schema_version": 2,
            "accounts": [{
                "account_id": "A", "name": "A账户", "strategy": "long_term_core",
                "baseline_date": "2026-09-11", "total_assets": 100,
                "cash": 50,
                "holdings": [{
                    "code": "159500", "name": "中证500ETF", "market": "CN",
                    "instrument_type": "etf", "valuation_mode": "exchange",
                    "cost_price": 1, "position_percent": 0.50,
                    "baseline_value": 50, "sector": "diversified", "theme": "mid_cap",
                }],
            }],
        }

    def test_parses_two_accounts_with_decimal_values(self):
        payload = self.valid_payload()
        payload["accounts"][0].update(total_assets="1000.00", cash="700.00")
        payload["accounts"][0]["holdings"][0].update(
            code="510300", name="合成指数样例", cost_price="1.2345",
            position_percent="0.30", baseline_value="300.00",
        )
        payload["accounts"].append({
            "account_id": "DEMO_B", "name": "合成演示账户二",
            "strategy": "active_equity", "baseline_date": "2026-01-05",
            "total_assets": "2000.50", "cash": "1500.50",
            "holdings": [{
                "code": "600000", "name": "合成股票样例", "market": "CN",
                "instrument_type": "stock", "valuation_mode": "exchange",
                "cost_price": "9.8765", "quantity": "50",
                "baseline_price": "10.000", "baseline_value": "500.00",
                "sector": "synthetic", "theme": "synthetic",
            }],
        })
        config = parse_portfolio(payload)
        self.assertEqual(config.schema_version, 2)
        self.assertEqual(config.accounts[0].total_assets, Decimal("1000.00"))
        self.assertEqual(config.accounts[1].cash, Decimal("1500.50"))
        self.assertEqual(config.accounts[1].holdings[0].quantity, Decimal("50"))
        self.assertEqual(config.accounts[1].holdings[0].cost_price, Decimal("9.8765"))
        self.assertEqual(config.accounts[0].holdings[0].position_percent, Decimal("0.30"))

    def test_rejects_duplicate_account_ids(self):
        payload = {
            "schema_version": 2,
            "accounts": [
                {"account_id": "A", "name": "one", "strategy": "x",
                 "baseline_date": "2026-09-11", "total_assets": 1,
                 "cash": 1, "holdings": []},
                {"account_id": "A", "name": "two", "strategy": "x",
                 "baseline_date": "2026-09-11", "total_assets": 1,
                 "cash": 1, "holdings": []},
            ],
        }
        with self.assertRaisesRegex(ValueError, "duplicate account_id"):
            parse_portfolio(payload)

    def test_rejects_account_that_does_not_reconcile(self):
        payload = {
            "schema_version": 2,
            "accounts": [{
                "account_id": "A", "name": "A账户", "strategy": "long_term_core",
                "baseline_date": "2026-09-11", "total_assets": 100,
                "cash": 20,
                "holdings": [{
                    "code": "159500", "name": "中证500ETF", "market": "CN",
                    "instrument_type": "etf", "valuation_mode": "exchange",
                    "cost_price": 1, "position_percent": 0.50,
                    "baseline_value": 50, "sector": "diversified", "theme": "mid_cap",
                }],
            }],
        }
        with self.assertRaisesRegex(ValueError, "does not reconcile"):
            parse_portfolio(payload)

    def test_accepts_legacy_flat_portfolio_as_read_only_compatibility(self):
        config = parse_portfolio({"portfolio": [{"name": "中证500ETF", "type": "core_index"}]})
        self.assertEqual(config.schema_version, 1)
        self.assertEqual(config.legacy_holdings[0]["name"], "中证500ETF")

    def test_loads_synthetic_example_without_production_configuration(self):
        config = load_portfolio(
            Path(__file__).parents[1] / "config" / "portfolio.example.yaml"
        )
        self.assertEqual(config.schema_version, 2)
        self.assertEqual(tuple(a.account_id for a in config.accounts), ("DEMO_A", "DEMO_B"))
        self.assertEqual(config.accounts[0].baseline_date, date(2026, 1, 5))
        self.assertEqual(config.accounts[0].total_assets, Decimal("1000.00"))
        self.assertEqual(config.accounts[0].cash, Decimal("700.00"))
        self.assertEqual(config.accounts[0].holdings[0].position_percent, Decimal("0.30"))
        self.assertEqual(config.accounts[1].total_assets, Decimal("2000.50"))
        self.assertEqual(config.accounts[1].cash, Decimal("1500.50"))
        self.assertEqual(config.accounts[1].holdings[0].quantity, Decimal("50"))
        self.assertEqual(config.accounts[1].holdings[0].baseline_price, Decimal("10.000"))
        for account in config.accounts:
            self.assertIn("合成", account.name)
            self.assertEqual(
                account.cash + sum(h.baseline_value for h in account.holdings),
                account.total_assets,
            )

    def test_load_portfolio_preserves_decimal_scalar_text_precision(self):
        yaml_text = """
schema_version: 2
accounts:
  - account_id: A
    name: A账户
    strategy: long_term_core
    baseline_date: "2026-09-11"
    total_assets: 1
    cash: 0
    holdings:
      - code: "159500"
        name: 中证500ETF
        market: CN
        instrument_type: etf
        valuation_mode: exchange
        cost_price: 0.100000000000000005
        baseline_value: 1
        sector: diversified
        theme: mid_cap
"""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.yaml"
            path.write_text(yaml_text, encoding="utf-8")
            config = load_portfolio(path)
        self.assertEqual(
            config.accounts[0].holdings[0].cost_price,
            Decimal("0.100000000000000005"),
        )

    def test_rejects_duplicate_holding_codes(self):
        payload = self.valid_payload()
        payload["accounts"][0]["holdings"].append(payload["accounts"][0]["holdings"][0].copy())
        with self.assertRaisesRegex(ValueError, "duplicate holding code"):
            parse_portfolio(payload)

    def test_rejects_invalid_position_percent(self):
        payload = self.valid_payload()
        payload["accounts"][0]["holdings"][0]["position_percent"] = 1.01
        with self.assertRaisesRegex(ValueError, "position_percent"):
            parse_portfolio(payload)

    def test_rejects_non_positive_quantity(self):
        payload = self.valid_payload()
        payload["accounts"][0]["holdings"][0]["quantity"] = 0
        with self.assertRaisesRegex(ValueError, "quantity must be positive"):
            parse_portfolio(payload)

    def test_rejects_zero_baseline_value(self):
        payload = self.valid_payload()
        payload["accounts"][0]["holdings"][0]["baseline_value"] = 0
        with self.assertRaisesRegex(ValueError, "baseline_value must be positive"):
            parse_portfolio(payload, allow_unbalanced=True)

    def test_rejects_unsupported_or_incomplete_schema(self):
        with self.assertRaisesRegex(ValueError, "schema_version"):
            parse_portfolio({"schema_version": 3, "accounts": []})
        with self.assertRaisesRegex(ValueError, "accounts"):
            parse_portfolio({"schema_version": 2, "portfolio": []})
        with self.assertRaisesRegex(ValueError, "schema_version"):
            parse_portfolio({"accounts": []})

    def test_rejects_malformed_structures(self):
        with self.assertRaisesRegex(ValueError, "accounts must be a list"):
            parse_portfolio({"schema_version": 2, "accounts": {}})
        payload = self.valid_payload()
        payload["accounts"][0]["holdings"] = {}
        with self.assertRaisesRegex(ValueError, "holdings must be a list"):
            parse_portfolio(payload)

    def test_rejects_blank_or_non_string_required_fields(self):
        for field in ("account_id", "name", "strategy", "baseline_date"):
            payload = self.valid_payload()
            payload["accounts"][0][field] = " " if field != "baseline_date" else 20260911
            with self.assertRaises(ValueError, msg=field):
                parse_portfolio(payload)

    def test_rejects_invalid_financial_values(self):
        for field, value, message in (
            ("cost_price", 0, "cost_price"),
            ("baseline_price", 0, "baseline_price"),
            ("baseline_value", -1, "baseline_value"),
        ):
            payload = self.valid_payload()
            payload["accounts"][0]["holdings"][0][field] = value
            with self.assertRaisesRegex(ValueError, message):
                parse_portfolio(payload, allow_unbalanced=True)

    def test_exports_and_immutable_nested_configuration(self):
        legacy_entry = {"name": "ETF", "meta": {"tag": "core"}}
        config = parse_portfolio({"portfolio": [legacy_entry]})
        with self.assertRaises(TypeError):
            config.legacy_holdings[0]["name"] = "changed"
        with self.assertRaises(TypeError):
            config.legacy_holdings[0]["meta"]["tag"] = "changed"
        legacy_entry["name"] = "changed"
        self.assertEqual(config.legacy_holdings[0]["name"], "ETF")

        holding = HoldingConfig("159500", "ETF", "CN", "etf", "exchange", Decimal("1"), Decimal("1"), "sector", "theme")
        holdings = [holding]
        account = AccountConfig("A", "A", "x", date(2026, 9, 11), Decimal("1"), Decimal("1"), holdings)
        holding_snapshot = HoldingSnapshot("A", holding, None, Decimal("0"), Decimal("0"), None, None, "exact")
        account_holdings = [holding_snapshot]
        account_snapshot = AccountSnapshot(account, account_holdings, Decimal("0"), Decimal("1"), Decimal("1"), Decimal("0"), ["account warning"])
        accounts = [account]
        snapshot_accounts = [account_snapshot]
        config = PortfolioConfig(2, accounts)
        snapshot = PortfolioSnapshot(snapshot_accounts, Decimal("1"), Decimal("0"), Decimal("1"), Decimal("0"), {"CN": Decimal("1")}, {}, ["portfolio warning"])
        self.assertIsInstance(config.accounts, tuple)
        self.assertIsInstance(account.holdings, tuple)
        self.assertIsInstance(account_snapshot.holdings, tuple)
        self.assertIsInstance(snapshot.accounts, tuple)
        self.assertIsInstance(account_snapshot.warnings, tuple)
        self.assertIsInstance(snapshot.warnings, tuple)
        holding_snapshot_with_warning = HoldingSnapshot(
            "A", holding, None, Decimal("0"), Decimal("0"), None, None, "exact", warnings=["holding warning"]
        )
        self.assertIsInstance(holding_snapshot_with_warning.warnings, tuple)
        accounts.append(account)
        holdings.append(holding)
        account_holdings.append(holding_snapshot)
        snapshot_accounts.append(account_snapshot)
        self.assertEqual(len(config.accounts), 1)
        self.assertEqual(len(account.holdings), 1)
        self.assertEqual(len(account_snapshot.holdings), 1)
        self.assertEqual(len(snapshot.accounts), 1)
        self.assertIsInstance(snapshot.exposure_by_sector, MappingProxyType)
        with self.assertRaises(TypeError):
            snapshot.exposure_by_sector["CN"] = Decimal("2")
        self.assertIsInstance(account.holdings, tuple)
        self.assertTrue(all(cls for cls in (HoldingConfig, HoldingSnapshot, AccountSnapshot, PortfolioConfig)))


if __name__ == "__main__":
    unittest.main()
