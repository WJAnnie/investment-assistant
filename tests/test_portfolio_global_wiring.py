"""Offline integration tests for global market wiring into portfolio reports."""

from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from app.market.global_markets import INSTRUMENTS, SourceObservation
from app.workflow.portfolio import (
    GLOBAL_MARKET_STAGES,
    _analysis_gaps,
    _global_market_payload,
    run_portfolio_report,
)
from tests.test_portfolio_workflow import _config, _Router, _valuation


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MORNING_TIME = datetime(2026, 9, 17, 9, 0, 0, tzinfo=SHANGHAI_TZ)
GLOBAL_TIME = datetime(2026, 9, 17, 6, 30, 0, tzinfo=SHANGHAI_TZ)
MIDDAY_TIME = datetime(2026, 9, 17, 11, 30, 0, tzinfo=SHANGHAI_TZ)

REDLINE_FORBIDDEN_TOKENS = (
    "manual_replay",
    "available_unverified",
    "RECENT",
    "STALE",
    "yahoo_chart",
    "fred_dgs10",
    "error_code",
    "provider",
    "None",
    "completed",
    "not_collected",
    "INVALID_OBSERVATION",
    "full_analysis_ready",
    "data_limits",
    "立即买入",
    "立即卖出",
    "自动执行交易",
)

MANDATORY_PHRASES = (
    "不自动下单",
    "人工操作提醒",
    "进入人工复核",
    "WAIT",
    "本人确认并手动执行",
)


def _build_valid_observations():
    """Build 8 valid overnight observations for the approved instruments."""
    observations = {}
    for symbol in INSTRUMENTS:
        if symbol == "^TNX":
            observations[symbol] = SourceObservation(
                symbol="^TNX",
                value=4.05,
                previous_value=4.00,
                source_as_of=None,
                session_date="2026-09-16",
                source="fred_dgs10",
            )
        else:
            observations[symbol] = SourceObservation(
                symbol=symbol,
                value=100.0,
                previous_value=99.0,
                source_as_of="2026-09-16T16:00:00-04:00",
                session_date="2026-09-16",
                source="yahoo_chart",
            )
    return observations


class FakeGlobalProvider:
    def __init__(self, observations=None, fail=False):
        self.observations = observations or {}
        self.fail = fail

    def fetch(self, symbol):
        if self.fail:
            raise RuntimeError("simulated provider failure")
        return self.observations.get(symbol)


class FakeTreasuryFallback:
    def __init__(self, observation=None, fail=False):
        self.observation = observation
        self.fail = fail

    def fetch(self, symbol):
        if self.fail:
            raise RuntimeError("simulated fallback failure")
        if symbol == "^TNX":
            return self.observation
        return None


def _run_stage(kind, at, global_provider=None, treasury_fallback=None, **kwargs):
    valuation_item = _valuation()
    router = _Router({"600000": valuation_item})
    return run_portfolio_report(
        kind,
        config_loader=_config,
        valuation_router=router,
        strategy_loader=lambda: {},
        analysis_enabled=False,
        clock=lambda: at,
        global_provider=global_provider,
        treasury_fallback=treasury_fallback,
        **kwargs,
    )


class PortfolioGlobalWiringTests(unittest.TestCase):
    def test_morning_with_eight_valid_observations(self):
        obs_map = _build_valid_observations()
        global_provider = FakeGlobalProvider(obs_map)
        treasury_fallback = FakeTreasuryFallback(obs_map["^TNX"])

        result = _run_stage(
            "morning",
            MORNING_TIME,
            global_provider=global_provider,
            treasury_fallback=treasury_fallback,
        )

        self.assertEqual(result["status"], "completed")
        self.assertIn("global_market", result)
        self.assertEqual(result["global_market"]["status"], "complete")

        report = result["report"]
        self.assertIn("🌍 隔夜全球市场", report)
        self.assertIn("标普500", report)
        self.assertIn("美国10年期国债收益率", report)
        self.assertIn("- 美股方向：", report)

        for token in REDLINE_FORBIDDEN_TOKENS:
            self.assertNotIn(token, report, f"Forbidden token {token!r} found in morning report")
        for phrase in MANDATORY_PHRASES:
            self.assertIn(phrase, report, f"Mandatory phrase {phrase!r} missing from morning report")

    def test_morning_with_providers_none(self):
        result = _run_stage(
            "morning",
            MORNING_TIME,
            global_provider=None,
            treasury_fallback=None,
        )

        self.assertEqual(result["status"], "completed")
        self.assertIn("global_market", result)
        self.assertEqual(result["global_market"]["status"], "not_collected")

        report = result["report"]
        self.assertIn("本阶段不采集", report)
        self.assertIn("global_market_source", result["analysis_gaps"])

    def test_midday_does_not_contain_global_market(self):
        obs_map = _build_valid_observations()
        global_provider = FakeGlobalProvider(obs_map)
        treasury_fallback = FakeTreasuryFallback(obs_map["^TNX"])

        result = _run_stage(
            "midday",
            MIDDAY_TIME,
            global_provider=global_provider,
            treasury_fallback=treasury_fallback,
        )

        self.assertEqual(result["status"], "completed")
        self.assertNotIn("global_market", result)
        self.assertNotIn("🌍 隔夜全球市场", result["report"])

    def test_provider_runtime_error_does_not_fail_report(self):
        global_provider = FakeGlobalProvider(fail=True)
        treasury_fallback = FakeTreasuryFallback(fail=True)

        result = _run_stage(
            "morning",
            MORNING_TIME,
            global_provider=global_provider,
            treasury_fallback=treasury_fallback,
        )

        self.assertIn(result["status"], ("completed", "partial"))
        self.assertNotEqual(result["status"], "failed")
        self.assertIn("global_market", result)

        report = result["report"]
        self.assertTrue(len(report) > 0)
        self.assertNotIn("error_code", report)
        self.assertNotIn("provider", report)
        self.assertNotIn("yahoo_chart", report)
        self.assertNotIn("simulated provider failure", report)

    def test_analysis_gaps_deduction(self):
        gaps = _analysis_gaps(None, None, "morning")
        expected = [
            "global_market_source",
            "industry_ranking",
            "fundamentals",
            "weekly_structure",
            "minute_structure_confirmation",
        ]
        self.assertEqual(gaps, expected)

        result = _run_stage(
            "morning",
            MORNING_TIME,
            global_provider=None,
            treasury_fallback=None,
        )
        self.assertEqual(result["analysis_gaps"], expected)

    def test_redline_regression_for_morning_and_global_stages(self):
        obs_map = _build_valid_observations()
        for stage, at in (("morning", MORNING_TIME), ("global", GLOBAL_TIME)):
            with self.subTest(stage=stage):
                global_provider = FakeGlobalProvider(obs_map)
                treasury_fallback = FakeTreasuryFallback(obs_map["^TNX"])

                result = _run_stage(
                    stage,
                    at,
                    global_provider=global_provider,
                    treasury_fallback=treasury_fallback,
                )
                report = result["report"]

                for token in REDLINE_FORBIDDEN_TOKENS:
                    self.assertNotIn(token, report, f"Forbidden token {token!r} in {stage} report")
                for phrase in MANDATORY_PHRASES:
                    self.assertIn(phrase, report, f"Mandatory phrase {phrase!r} missing in {stage} report")

    def test_intraday_stages_do_not_claim_a_global_market_gap(self):
        """A stage that never collects the overnight block must not report it missing."""
        overnight = ("global", "morning")
        for kind in ("midday", "trading", "closing"):
            with self.subTest(kind=kind):
                gaps = _analysis_gaps(None, None, kind)
                self.assertNotIn("global_market_source", gaps)
                result = _run_stage(kind, MIDDAY_TIME)
                self.assertNotIn("global_market", result)
                self.assertNotIn("global_market_source", result["analysis_gaps"])
        for kind in overnight:
            with self.subTest(kind=kind):
                # The stage genuinely collects it, so an absent block is a real gap.
                self.assertIn("global_market_source", _analysis_gaps(None, None, kind))

        # A completed overnight block must clear the gap entirely.
        obs_map = _build_valid_observations()
        result = _run_stage(
            "morning", MORNING_TIME,
            global_provider=FakeGlobalProvider(obs_map),
            treasury_fallback=FakeTreasuryFallback(obs_map["^TNX"]),
        )
        self.assertEqual(result["global_market"]["status"], "complete")
        self.assertNotIn("global_market_source", result["analysis_gaps"])
