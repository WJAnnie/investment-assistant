import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from app.portfolio.models import (
    AccountConfig,
    HoldingConfig,
    PortfolioConfig,
    Valuation,
)
from app.workflow.portfolio import _workflow_time, run_portfolio_report


NOW = datetime(2026, 9, 11, 7, 30, tzinfo=timezone.utc)


def _config():
    holding = HoldingConfig(
        code="600000",
        name="浦发银行",
        market="CN",
        instrument_type="stock",
        valuation_mode="exchange",
        cost_price=Decimal("9"),
        baseline_value=Decimal("1000"),
        sector="financials",
        theme="bank",
        quantity=Decimal("100"),
        baseline_price=Decimal("10"),
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


def _valuation(code="600000", freshness="fresh", error=None):
    return Valuation(
        code=code,
        price=Decimal("10" if freshness == "fresh" else "0"),
        change_percent=Decimal("1" if freshness == "fresh" else "0"),
        as_of=NOW,
        source="fixture",
        freshness=freshness,
        error=error,
    )


class _Router:
    def __init__(self, valuations):
        self.valuations = valuations
        self.calls = []

    def value_portfolio(self, config, include_hstech=True):
        self.calls.append((config, include_hstech))
        return self.valuations


class _Notifier:
    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        if self.error is not None:
            raise self.error
        return self.result


class PortfolioWorkflowTests(unittest.TestCase):
    def test_default_report_time_uses_shanghai_timezone(self):
        with patch("app.workflow.portfolio._workflow_time", wraps=_workflow_time) as clock_samples:
            result = run_portfolio_report(
                "closing",
                config_loader=_config,
                valuation_router=_Router({"600000": _valuation()}),
                strategy_loader=lambda: {},
                analysis_enabled=False,
            )

        self.assertEqual(clock_samples.call_count, 3)
        for call in clock_samples.call_args_list:
            self.assertEqual(call.args[0].tzinfo, ZoneInfo("Asia/Shanghai"))
        self.assertEqual(result["status"], "completed")

    def test_runs_complete_pipeline_loads_once_and_sends_once(self):
        config = _config()
        loader = Mock(return_value=config)
        strategy_loader = Mock(return_value={})
        router = _Router({"600000": _valuation()})
        notifier = _Notifier()

        result = run_portfolio_report(
            "closing",
            config_loader=loader,
            valuation_router=router,
            notifier=notifier,
            now=NOW,
            strategy_loader=strategy_loader,
            analysis_enabled=False,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["report_kind"], "closing")
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["notified"])
        self.assertIn("A账户", result["report"])
        self.assertIn("全部账户", result["report"])
        loader.assert_called_once_with()
        strategy_loader.assert_called_once_with()
        self.assertEqual(router.calls, [(config, True)])
        self.assertEqual(notifier.messages, [result["report"]])

    def test_stale_or_lagged_valuation_marks_result_partial(self):
        for freshness in ("stale", "lagged"):
            with self.subTest(freshness=freshness):
                result = run_portfolio_report(
                    "trading",
                    config_loader=_config,
                    valuation_router=_Router({
                        "600000": _valuation("600000", freshness),
                    }),
                    now=NOW,
                    strategy_loader=lambda: {},
                    analysis_enabled=False,
                )

                self.assertEqual(result["status"], "partial")
                self.assertTrue(any(freshness in error for error in result["errors"]))
                self.assertIn("WAIT", result["report"])

    def test_unavailable_analysis_marks_result_partial(self):
        result = run_portfolio_report(
            "trading",
            config_loader=_config,
            valuation_router=_Router({"600000": _valuation()}),
            now=NOW,
            strategy_loader=lambda: {},
        )

        self.assertEqual(result["analysis"]["coverage"]["unavailable"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("analysis:" in error for error in result["errors"]))

    def test_failed_valuation_returns_partial_and_keeps_baseline_report(self):
        router = _Router({
            "600000": _valuation("600000", "failed", "provider offline"),
        })

        result = run_portfolio_report(
            "morning",
            config_loader=_config,
            valuation_router=router,
            now=NOW,
            strategy_loader=lambda: {},
        )

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["notified"])
        self.assertIn("600000: provider offline", result["errors"])
        self.assertIn("基准值估算", result["report"])
        self.assertEqual(result["snapshot"].total_assets, Decimal("1500.00"))

    def test_false_or_raising_notification_is_nonfatal_and_attempted_once(self):
        for notifier, expected in (
            (_Notifier(result=False), "message was not sent"),
            (_Notifier(error=RuntimeError("secret detail")), "send failed"),
        ):
            with self.subTest(expected=expected):
                result = run_portfolio_report(
                    "closing",
                    config_loader=_config,
                    valuation_router=_Router({"600000": _valuation()}),
                    notifier=notifier,
                    now=NOW,
                    strategy_loader=lambda: {},
                )
                self.assertEqual(result["status"], "partial")
                self.assertFalse(result["notified"])
                self.assertEqual(len(notifier.messages), 1)
                self.assertTrue(any(expected in error for error in result["errors"]))
                self.assertNotIn("secret detail", " ".join(result["errors"]))
                self.assertTrue(result["report"])

    def test_fatal_workflow_error_returns_structured_failed_result(self):
        result = run_portfolio_report(
            "closing",
            config_loader=Mock(side_effect=ValueError("broken config")),
            valuation_router=_Router({}),
            now=NOW,
            strategy_loader=lambda: {},
        )

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["snapshot"])
        self.assertIn("数据未就绪", result["report"])
        self.assertIn("WAIT", result["report"])
        self.assertIn("不自动下单", result["report"])
        self.assertEqual(result["errors"][0], "workflow: ValueError")
        self.assertNotIn("broken config", str(result))

    def test_fatal_workflow_sends_safe_wait_reminder_once(self):
        notifier = _Notifier()

        result = run_portfolio_report(
            "trading",
            config_loader=Mock(side_effect=ValueError("broken config")),
            valuation_router=_Router({}),
            notifier=notifier,
            now=NOW,
            strategy_loader=lambda: {},
        )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["notified"])
        self.assertEqual(notifier.messages, [result["report"]])
        self.assertIn("数据未就绪", result["report"])
        self.assertIn("WAIT", result["report"])
        self.assertIn("本人确认并手动执行", result["report"])

    def test_fatal_workflow_notification_failure_is_sanitized_and_nonfatal(self):
        notifier = _Notifier(error=RuntimeError("secret transport detail"))

        result = run_portfolio_report(
            "trading",
            config_loader=Mock(side_effect=ValueError("broken config")),
            valuation_router=_Router({}),
            notifier=notifier,
            now=NOW,
            strategy_loader=lambda: {},
        )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["notified"])
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("notification: send failed", result["errors"])
        self.assertNotIn("secret transport detail", " ".join(result["errors"]))
        self.assertNotIn("secret transport detail", result["report"])

    def test_only_explicit_true_notification_response_counts_as_delivered(self):
        for response in ("false", 1, {"ok": False}):
            for fatal in (False, True):
                with self.subTest(response=response, fatal=fatal):
                    notifier = _Notifier(result=response)
                    loader = Mock(side_effect=ValueError("broken config")) if fatal else _config
                    result = run_portfolio_report(
                        "trading",
                        config_loader=loader,
                        valuation_router=_Router({"600000": _valuation()}),
                        notifier=notifier,
                        now=NOW,
                        strategy_loader=lambda: {},
                        analysis_enabled=False,
                    )
                    self.assertIs(result["notified"], False)
                    self.assertEqual(result["status"], "failed" if fatal else "partial")
                    self.assertEqual(len(notifier.messages), 1)

    def test_unknown_report_kind_fails_closed_without_masking_the_result(self):
        result = run_portfolio_report(
            "unknown",
            config_loader=Mock(side_effect=ValueError("broken config")),
            valuation_router=_Router({}),
            now=NOW,
            strategy_loader=lambda: {},
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["report_kind"], "unknown")
        self.assertIn("未知报告阶段", result["report"])
        self.assertIn("WAIT", result["report"])

    def test_postprocessing_error_still_returns_and_sends_failure_reminder(self):
        class ExplodingCoverage(dict):
            def __init__(self):
                super().__init__(ready=1, total=1, unavailable=0)
                self.calls = 0

            def get(self, key, default=None):
                self.calls += 1
                if self.calls > 3:
                    raise TypeError("malformed coverage")
                return super().get(key, default)

        analysis = {
            "items": (),
            "benchmark": None,
            "coverage": ExplodingCoverage(),
        }
        notifier = _Notifier()
        with patch("app.workflow.portfolio.analyze_portfolio", return_value=analysis):
            result = run_portfolio_report(
                "trading",
                config_loader=_config,
                valuation_router=_Router({"600000": _valuation()}),
                notifier=notifier,
                now=NOW,
                strategy_loader=lambda: {},
            )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["notified"])
        self.assertIn("数据未就绪", result["report"])
        self.assertEqual(notifier.messages, [result["report"]])


if __name__ == "__main__":
    unittest.main()
