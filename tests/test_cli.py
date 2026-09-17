import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal
from io import StringIO
from types import MappingProxyType
from unittest.mock import patch

from app import main as main_module


class CliTests(unittest.TestCase):
    def test_print_json_uses_ascii_fallback_for_legacy_console_encoding(self):
        class LegacyConsole(StringIO):
            encoding = "gbk"

        output = LegacyConsole()

        with patch.object(main_module.sys, "stdout", output):
            main_module._print_json({"report": "📊 A账户"})

        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["report"], "📊 A账户")

    def test_json_safe_serializes_read_only_dataclass_mappings(self):
        from app.portfolio.models import PortfolioSnapshot

        snapshot = PortfolioSnapshot(
            accounts=(),
            total_assets=Decimal("10.00"),
            holdings_value=Decimal("4.00"),
            cash=Decimal("6.00"),
            position_percent=Decimal("0.4000"),
            exposure_by_market={"CN": Decimal("0.4000")},
        )

        parsed = main_module._json_safe(snapshot)

        self.assertEqual(parsed["total_assets"], "10.00")
        self.assertEqual(parsed["exposure_by_market"], {"CN": "0.4000"})

    def test_main_runs_batch_for_multiple_codes_and_prints_json_summary(self):
        batch_result = {
            "status": "completed",
            "total": 2,
            "succeeded": 2,
            "failed": 0,
            "results": [
                {"code": "000001", "status": "completed"},
                {"code": "510300", "status": "completed"},
            ],
        }
        fake_collector = object()
        output = StringIO()

        with patch(
            "app.main.create_default_collector", create=True, return_value=fake_collector
        ), patch(
            "app.main.run_daily_reports", create=True, return_value=batch_result
        ) as run_reports, redirect_stdout(output):
            exit_code = main_module.main(
                [
                    "--code",
                    "000001",
                    "--code",
                    "510300",
                    "--start",
                    "20260901",
                    "--end",
                    "20260911",
                    "--no-notify",
                ]
            )

        self.assertEqual(exit_code, 0)
        run_reports.assert_called_once_with(
            ["000001", "510300"],
            collector=fake_collector,
            notifier=None,
            history_start="20260901",
            history_end="20260911",
            history_period="daily",
            history_adjust="",
        )
        self.assertEqual(json.loads(output.getvalue()), batch_result)

    def test_main_returns_nonzero_when_any_code_fails(self):
        batch_result = {
            "status": "partial",
            "total": 1,
            "succeeded": 0,
            "failed": 1,
            "results": [{"code": "000001", "status": "failed", "error": "offline"}],
        }

        with patch("app.main.create_default_collector", create=True, return_value=object()), patch(
            "app.main.run_daily_reports", create=True, return_value=batch_result
        ), redirect_stdout(StringIO()):
            exit_code = main_module.main(["--code", "000001", "--no-notify"])

        self.assertEqual(exit_code, 1)

    def test_main_supports_snapshot_mode_without_codes(self):
        snapshot_result = {
            "status": "completed",
            "total": 4,
            "succeeded": 4,
            "failed": 0,
            "quotes": [],
            "report": "snapshot",
            "notified": False,
        }
        fake_collector = object()

        with patch("app.main.SinaProvider", create=True), patch(
            "app.main.MarketCollector", create=True, return_value=fake_collector
        ), patch(
            "app.main.run_market_snapshot", create=True, return_value=snapshot_result
        ) as run_snapshot, redirect_stdout(StringIO()):
            exit_code = main_module.main(["--snapshot", "--no-notify"])

        self.assertEqual(exit_code, 0)
        run_snapshot.assert_called_once_with(
            main_module.DEFAULT_MARKET_CODES,
            collector=fake_collector,
            notifier=None,
        )

    def test_main_supports_schedule_mode(self):
        with patch("app.main.start_scheduler", create=True) as start_scheduler:
            exit_code = main_module.main(["--schedule"])

        self.assertEqual(exit_code, 0)
        start_scheduler.assert_called_once_with()

    def test_main_supports_portfolio_mode_and_serializes_rich_values(self):
        result = {
            "status": "partial",
            "errors": ["one source failed"],
            "notified": False,
            "amount": Decimal("4321.09"),
            "as_of": datetime(2026, 9, 11, 15, 30, tzinfo=timezone.utc),
            "baseline_date": date(2026, 9, 11),
            "exposure": MappingProxyType({"CN": Decimal("0.7500")}),
        }
        output = StringIO()
        sentinel_fundamental = object()

        with patch("app.main.FeishuNotifier", create=True) as notifier_type, patch(
            "app.main.run_portfolio_report", create=True, return_value=result
        ) as run_report, patch(
            "app.main.create_fundamental_provider", return_value=sentinel_fundamental
        ) as create_fundamental, redirect_stdout(output):
            exit_code = main_module.main(
                ["--portfolio", "--report-kind", "closing", "--no-notify"]
            )

        self.assertEqual(exit_code, 0)
        notifier_type.assert_not_called()
        create_fundamental.assert_called_once_with()
        run_report.assert_called_once_with(
            report_kind="closing",
            notifier=None,
            fundamental_provider=sentinel_fundamental,
        )
        self.assertNotIn("global_provider", run_report.call_args.kwargs)
        self.assertNotIn("treasury_fallback", run_report.call_args.kwargs)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["amount"], "4321.09")
        self.assertEqual(parsed["as_of"], "2026-09-11T15:30:00+00:00")
        self.assertEqual(parsed["baseline_date"], "2026-09-11")
        self.assertEqual(parsed["exposure"], {"CN": "0.7500"})

    def test_main_returns_one_for_fatal_portfolio_result(self):
        result = {"status": "failed", "errors": ["bad config"], "notified": False}
        with patch("app.main.run_portfolio_report", return_value=result), redirect_stdout(StringIO()):
            self.assertEqual(main_module.main(["--portfolio", "--no-notify"]), 1)

    def test_portfolio_mode_is_mutually_exclusive_with_other_modes_and_codes(self):
        for args in (
            ["--portfolio", "--snapshot"],
            ["--portfolio", "--schedule"],
            ["--portfolio", "--code", "000001"],
            ["--report-kind", "morning", "--code", "000001"],
        ):
            with self.subTest(args=args), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main_module.main(args)
                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
