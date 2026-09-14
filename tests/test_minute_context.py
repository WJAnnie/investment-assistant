"""Offline minute evidence and the manual-report integration contract."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.integration.github_export import build_manifest, build_snapshot, write_json_atomic
from app.market.minute.context import MinuteSnapshot, load_minute_context
from app.market.minute.session import cn_trading_day
from app.portfolio.analysis import analyze_portfolio
from app.portfolio.calculator import build_portfolio_snapshot
from app.portfolio.models import AccountConfig, HoldingConfig, PortfolioConfig, Valuation
from app.report.portfolio import format_portfolio_report
from app.workflow.portfolio import run_portfolio_report


TZ = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 9, 14)
CYCLES = {"5m", "15m", "30m", "120m"}
ENVIRONMENT = {
    "GITHUB_REPOSITORY": "fixture/investment-assistant",
    "GITHUB_WORKFLOW": "offline minute contract",
    "GITHUB_RUN_ID": "1",
    "GITHUB_SHA": "fixture-sha",
}


def _at(hour, minute=0, second=0):
    return datetime(2026, 9, 14, hour, minute, second, tzinfo=TZ)


def _holding(**changes):
    holding = HoldingConfig(
        code="600000", name="测试持仓", market="CN", instrument_type="stock",
        valuation_mode="exchange", cost_price=Decimal("9"),
        baseline_value=Decimal("100"), baseline_price=Decimal("10"),
        sector="financials", theme="bank", quantity=Decimal("10"),
    )
    return replace(holding, **changes)


def _config(holdings=None):
    return PortfolioConfig(schema_version=2, accounts=(AccountConfig(
        account_id="A", name="A账户", strategy="long_term_core",
        baseline_date=DAY, total_assets=Decimal("1000"), cash=Decimal("900"),
        holdings=tuple(holdings or (_holding(),)),
    ),))


def _minute_lines(until):
    # Independent fixed fixture, not the resampler's session/grid generator.
    result = []
    for opening in (_at(9, 30), _at(13)):
        for minutes in range(5, 121, 5):
            timestamp = opening + timedelta(minutes=minutes)
            if timestamp <= until:
                result.append(KLine(timestamp.isoformat(), 10, 12, 9, 11, 100))
    return result


def _minute_snapshot(now=None, **changes):
    now = now or _at(14, 30)
    snapshot = MinuteSnapshot(
        code="600000", market="CN",
        day=cn_trading_day(DAY, is_open=True, source="explicit calendar fixture"),
        lines=_minute_lines(now), source="offline minute fixture", fetched_at=now,
        base_minutes=5, timestamp_semantics="close",
    )
    return replace(snapshot, **changes)


def _daily_lines():
    return [
        KLine((date(2026, 8, 1) + timedelta(days=index)).isoformat(),
              100 + index, 101 + index, 99 + index, 100 + index, 1000)
        for index in range(35)
    ]


class _Router:
    def __init__(self, now):
        self.now = now

    def value_portfolio(self, config, include_hstech=True):
        return {
            holding.code: Valuation(
                holding.code, Decimal("10"), Decimal("1"), self.now, "fixture",
                freshness="fresh",
            )
            for account in config.accounts for holding in account.holdings
        }

    def get_klines_for_holding(self, _holding, **_kwargs):
        return _daily_lines()

    def get_klines(self, _code, **_kwargs):
        return _daily_lines()


class MinuteContextTests(unittest.TestCase):
    def _context(self, snapshot, *, now=None):
        return load_minute_context(
            _holding(), snapshot_loader=lambda _holding, **_kwargs: snapshot,
            now=now or _at(14, 30),
        )

    def test_unconfigured_source_is_explicit_and_never_ready(self):
        context = load_minute_context(_holding(), now=_at(14, 30))
        self.assertEqual(context["status"], "unavailable")
        self.assertFalse(context["configured"])
        self.assertIn("未接入", context["reason"])
        self.assertEqual(context["bar_status"], dict.fromkeys(CYCLES, "missing"))
        self.assertFalse(context["input_ready"])
        self.assertFalse(context["multi_cycle_confirm"])
        self.assertIsNone(context["next_trigger"]["at"])

    def test_unsupported_market_and_nav_do_not_call_loader(self):
        for changes in ({"market": "HK"}, {"market": "GLOBAL"},
                        {"valuation_mode": "fund_nav"}):
            with self.subTest(changes=changes):
                loader = Mock()
                context = load_minute_context(
                    _holding(**changes), snapshot_loader=loader, now=_at(14, 30),
                )
                self.assertEqual(context["status"], "unavailable")
                self.assertTrue(context["configured"])
                self.assertFalse(context["input_ready"])
                loader.assert_not_called()

    def test_1430_waits_for_afternoon_120m_and_arrival_of_all_data(self):
        context = self._context(_minute_snapshot())
        self.assertEqual(context["status"], "ready")
        self.assertEqual(context["bar_status"]["120m"], "forming")
        self.assertEqual(context["cycles"]["120m"]["observed"], 18)
        self.assertEqual(context["cycles"]["120m"]["expected"], 24)
        self.assertFalse(context["input_ready"])
        self.assertFalse(context["multi_cycle_confirm"])
        self.assertEqual(context["next_trigger"]["at"], _at(15).isoformat())
        self.assertIn("数据齐全", context["next_trigger"]["condition"])
        self.assertIn("人工复核", context["next_trigger"]["action"])

    def test_all_closed_only_means_input_ready_not_a_buy_confirmation(self):
        context = self._context(_minute_snapshot(_at(15)), now=_at(15))
        self.assertEqual(context["bar_status"], dict.fromkeys(CYCLES, "closed"))
        self.assertTrue(context["input_ready"])
        self.assertFalse(context["multi_cycle_confirm"])
        self.assertIn("不代表", context["reason"])
        self.assertIsNone(context["next_trigger"]["at"])

    def test_delayed_last_bar_stays_missing_even_after_wall_clock_close(self):
        snapshot = _minute_snapshot(_at(15), lines=_minute_lines(_at(14, 55)))
        context = self._context(snapshot, now=_at(15))
        self.assertEqual(context["status"], "unavailable")
        self.assertEqual(context["bar_status"]["120m"], "missing")
        self.assertFalse(context["input_ready"])
        self.assertIsNone(context["next_trigger"]["at"])
        self.assertIn("数据齐全", context["next_trigger"]["condition"])

    def test_morning_gap_is_not_hidden_by_complete_afternoon_bars(self):
        snapshot = _minute_snapshot(_at(15))
        context = self._context(replace(snapshot, lines=snapshot.lines[1:]), now=_at(15))
        self.assertEqual(context["status"], "unavailable")
        self.assertEqual(context["cycles"]["120m"]["current_status"], "closed")
        self.assertEqual(context["bar_status"]["120m"], "missing")
        self.assertFalse(context["input_ready"])

    def test_lunch_targets_new_session_and_1300_does_not_reuse_morning_close(self):
        for current in (_at(11, 30), _at(12, 30)):
            with self.subTest(current=current):
                context = self._context(_minute_snapshot(current), now=current)
                self.assertEqual(context["bar_status"]["120m"], "closed")
                self.assertEqual(context["next_trigger"]["at"], _at(13).isoformat())
        context = self._context(_minute_snapshot(_at(13)), now=_at(13))
        self.assertEqual(context["bar_status"]["120m"], "forming")
        self.assertEqual(context["next_trigger"]["at"], _at(15).isoformat())

    def test_preopen_and_verified_closed_day_never_infer_next_trading_day(self):
        context = self._context(_minute_snapshot(_at(9)), now=_at(9))
        self.assertFalse(context["input_ready"])
        self.assertEqual(context["next_trigger"]["at"], _at(9, 30).isoformat())
        closed_day = cn_trading_day(DAY, is_open=False, source="explicit closure fixture")
        context = self._context(_minute_snapshot(day=closed_day, lines=[]))
        self.assertIn("休市", context["reason"])
        self.assertFalse(context["input_ready"])
        self.assertIsNone(context["next_trigger"]["at"])

    def test_verified_half_day_never_invents_an_afternoon_review_time(self):
        day = cn_trading_day(DAY, is_open=True, half_day=True, source="half-day fixture")
        for current in (_at(11, 30), _at(14, 30), _at(16, 10)):
            with self.subTest(current=current):
                snapshot = _minute_snapshot(
                    current, day=day, lines=_minute_lines(_at(11, 30)),
                    fetched_at=_at(11, 30),
                )
                context = self._context(snapshot, now=current)
                self.assertEqual(context["bar_status"], dict.fromkeys(CYCLES, "closed"))
                self.assertTrue(context["input_ready"])
                self.assertFalse(context["multi_cycle_confirm"])
                self.assertIsNone(context["next_trigger"]["at"])
                self.assertIn("下一交易日", context["next_trigger"]["condition"])

    def test_snapshot_becomes_missing_at_next_due_bar_not_just_after_fetch(self):
        snapshot = _minute_snapshot(_at(14, 25))
        for current, expected in ((_at(14, 29, 59), "ready"),
                                  (_at(14, 30), "unavailable"),
                                  (_at(14, 30, 1), "unavailable")):
            with self.subTest(current=current):
                context = self._context(snapshot, now=current)
                self.assertEqual(context["status"], expected)
                self.assertFalse(context["input_ready"])
                self.assertEqual(context["cycles"]["120m"]["observed"], 17)
                if expected == "unavailable":
                    self.assertEqual(set(context["bar_status"].values()), {"missing"})

    def test_missing_or_malformed_snapshot_metadata_fails_closed(self):
        snapshot = _minute_snapshot()
        invalid = (
            {"code": "600001"}, {"code": ""}, {"market": "HK"}, {"day": None},
            {"day": cn_trading_day(date(2026, 9, 11), is_open=True, source="fixture")},
            {"source": "  "}, {"source": None},
            {"fetched_at": _at(14, 29)}, {"fetched_at": _at(14, 31)},
            {"fetched_at": datetime(2026, 9, 14, 14, 30)}, {"fetched_at": None},
            {"base_minutes": True}, {"base_minutes": 1}, {"base_minutes": 5.0},
            {"base_minutes": "5"}, {"timestamp_semantics": "open"},
            {"timestamp_semantics": None}, {"lines": None},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                context = self._context(replace(snapshot, **changes))
                self.assertEqual(context["status"], "unavailable")
                self.assertEqual(set(context["bar_status"].values()), {"invalid"})
                self.assertFalse(context["input_ready"])
                self.assertIsNone(context["next_trigger"]["at"])
        self.assertEqual(self._context({})["status"], "unavailable")
        self.assertEqual(self._context(None)["status"], "unavailable")

    def test_provider_exception_does_not_expose_its_message(self):
        marker = "synthetic-private-provider-detail"
        loader = Mock(side_effect=RuntimeError("api_key=" + marker))
        context = load_minute_context(_holding(), snapshot_loader=loader, now=_at(14, 30))
        self.assertEqual(context["status"], "unavailable")
        self.assertEqual(set(context["bar_status"].values()), {"missing"})
        self.assertNotIn(marker, repr(context))
        loader.assert_called_once_with(_holding(), now=_at(14, 30))

    def test_sources_are_redacted_and_snapshot_is_not_mutated(self):
        snapshot = _minute_snapshot(source="fixture?api_key=synthetic-label-secret")
        before = deepcopy(snapshot)
        context = self._context(snapshot)
        self.assertNotIn("synthetic-label-secret", repr(context))
        self.assertIn("[REDACTED]", context["source"])
        self.assertEqual(snapshot, before)
        with self.assertRaises(FrozenInstanceError):
            snapshot.source = "changed"

    def test_calendar_and_session_labels_are_redacted_in_serializable_context(self):
        snapshot = _minute_snapshot()
        day = replace(
            snapshot.day, source="calendar?api_key=synthetic-calendar-label",
            sessions=tuple(
                replace(session, name=f"{session.name}?token=synthetic-session-label-{index}")
                for index, session in enumerate(snapshot.day.sessions)
            ),
        )
        snapshot = replace(snapshot, day=day)
        before = deepcopy(snapshot)
        context = self._context(snapshot)
        self.assertEqual(context["status"], "ready")
        self.assertNotIn("synthetic-calendar-label", repr(context))
        self.assertNotIn("synthetic-session-label", repr(context))
        self.assertIn("[REDACTED]", context["cycles"]["120m"]["session"])
        self.assertEqual(snapshot, before)

    def test_utc_fetch_and_bar_timestamps_use_same_shanghai_session(self):
        snapshot = _minute_snapshot()
        snapshot = replace(
            snapshot,
            lines=[replace(line, time=datetime.fromisoformat(line.time).astimezone(timezone.utc))
                   for line in snapshot.lines],
            fetched_at=_at(14, 30).astimezone(timezone.utc),
        )
        context = self._context(snapshot, now=_at(14, 30).astimezone(timezone.utc))
        self.assertEqual(context["bar_status"]["120m"], "forming")
        self.assertEqual(context["fetched_at"], _at(14, 30).isoformat())

    def test_naive_analysis_time_is_rejected_before_loading(self):
        loader = Mock()
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            load_minute_context(_holding(), snapshot_loader=loader,
                                now=datetime(2026, 9, 14, 14, 30))
        loader.assert_not_called()


class MinutePortfolioIntegrationTests(unittest.TestCase):
    def _run(self, *, now=None, loader=None, enabled=True, kind="trading"):
        current = now or _at(14, 30)
        return run_portfolio_report(
            kind, config_loader=_config, valuation_router=_Router(current),
            now=current, strategy_loader=lambda: {}, analysis_enabled=enabled,
            minute_snapshot_loader=loader,
        )

    def test_no_minute_loader_preserves_daily_contract_and_states_the_limit(self):
        result = self._run()
        item = result["analysis"]["items"][0]
        self.assertEqual(item["bar_status"], "complete")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(item["minute_context"]["configured"])
        self.assertIn("分钟数据：未接入", result["report"])

    def test_workflow_injects_loader_and_trading_report_shows_unwarned_holding(self):
        loader = Mock(return_value=_minute_snapshot())
        result = self._run(loader=loader)
        self.assertEqual(result["status"], "completed")
        item = result["analysis"]["items"][0]
        self.assertEqual(item["bar_status"]["daily"], "complete")
        self.assertEqual(item["bar_status"]["120m"], "forming")
        self.assertEqual(item["next_trigger"], item["minute_context"]["next_trigger"])
        self.assertFalse(item["decision"]["auto_execute"])
        self.assertNotIn(item["decision"]["action"], {"BUY", "ADD"})
        self.assertIn("600000", result["report"])
        self.assertIn("分钟复核", result["report"])
        self.assertIn("120m 形成中", result["report"])
        self.assertIn("15:00", result["report"])
        self.assertIn("数据齐全", result["report"])
        self.assertIn("不自动下单", result["report"])
        self.assertNotIn("暂无已触发", result["report"])
        loader.assert_called_once_with(_holding(), now=_at(14, 30))

    def test_disabling_analysis_does_not_fetch_minute_data(self):
        loader = Mock()
        result = self._run(loader=loader, enabled=False)
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["analysis"])
        loader.assert_not_called()

    def test_minute_failure_isolated_from_other_holdings_and_daily_analysis(self):
        config = _config((_holding(), _holding(code="600001")))

        def loader(holding, **_kwargs):
            if holding.code == "600000":
                raise RuntimeError("password=synthetic-loader-detail")
            return _minute_snapshot(code=holding.code)

        snapshot = build_portfolio_snapshot(config, {})
        analysis = analyze_portfolio(
            config, snapshot, history_loader=lambda _target, **_kwargs: _daily_lines(),
            now=_at(14, 30), minute_snapshot_loader=loader,
        )
        self.assertEqual([item["status"] for item in analysis["items"]], ["ready", "ready"])
        self.assertEqual([item["minute_context"]["status"] for item in analysis["items"]],
                         ["unavailable", "ready"])
        self.assertEqual(analysis["minute_coverage"], {"ready": 1, "total": 2, "unavailable": 1})
        report = format_portfolio_report(snapshot, "trading", _at(14, 30), analysis=analysis)
        self.assertIn("600001", report)
        self.assertNotIn("synthetic-loader-detail", report)
        self.assertFalse(any(item["decision"]["auto_execute"] for item in analysis["items"]))

    def test_cache_reuses_same_instrument_but_keeps_valuation_modes_separate(self):
        for mode, expected in (("exchange", "ready"), ("fund_nav", "unavailable")):
            with self.subTest(mode=mode):
                original = _config()
                account_b = replace(
                    original.accounts[0], account_id="B", name="B账户",
                    holdings=(_holding(valuation_mode=mode),),
                )
                config = replace(original, accounts=original.accounts + (account_b,))
                loader = Mock(return_value=_minute_snapshot())
                analysis = analyze_portfolio(
                    config, build_portfolio_snapshot(config, {}), now=_at(14, 30),
                    history_loader=lambda _holding, **_kwargs: _daily_lines(),
                    minute_snapshot_loader=loader,
                )
                self.assertEqual([item["account_id"] for item in analysis["items"]],
                                 ["A", "B"])
                self.assertEqual([item["minute_context"]["status"]
                                  for item in analysis["items"]], ["ready", expected])
                loader.assert_called_once_with(_holding(), now=_at(14, 30))
                self.assertFalse(any(item["decision"]["auto_execute"]
                                     for item in analysis["items"]))

    def test_daily_failure_does_not_hide_valid_minute_wait_reminder(self):
        config = _config()
        analysis = analyze_portfolio(
            config, build_portfolio_snapshot(config, {}), now=_at(14, 30),
            minute_snapshot_loader=lambda _holding, **_kwargs: _minute_snapshot(),
        )
        item = analysis["items"][0]
        self.assertEqual(item["status"], "unavailable")
        self.assertEqual(item["minute_context"]["bar_status"]["120m"], "forming")
        self.assertIsNone(item["decision"])

    def test_missing_minute_inputs_make_workflow_partial_but_preserve_daily_results(self):
        result = self._run(loader=lambda _holding, **_kwargs: _minute_snapshot(lines=[]))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["analysis"]["coverage"]["ready"], 1)
        self.assertEqual(result["analysis"]["minute_coverage"]["unavailable"], 1)
        self.assertTrue(any("minute" in error for error in result["errors"]))
        self.assertIn("WAIT", result["report"])

    def test_full_closure_never_promotes_decision_or_claims_multicycle_confirmation(self):
        result = self._run(now=_at(15), loader=lambda _holding, **_kwargs: _minute_snapshot(_at(15)))
        item = result["analysis"]["items"][0]
        self.assertTrue(item["minute_context"]["input_ready"])
        self.assertFalse(item["minute_context"]["multi_cycle_confirm"])
        self.assertNotIn(item["decision"]["action"], {"BUY", "ADD"})
        self.assertFalse(item["decision"]["auto_execute"])
        self.assertIn("不代表", result["report"])

    def test_export_readiness_is_not_minute_closure_or_a_buy_confirmation(self):
        cases = (
            ("trading", _at(14, 30), _minute_snapshot(), "FORMING", False),
            ("closing", _at(16, 10), _minute_snapshot(_at(15)), "CLOSED", True),
        )
        for kind, current, snapshot, expected_bar, expected_input_ready in cases:
            with self.subTest(kind=kind):
                result = self._run(
                    kind=kind, now=current, loader=lambda _holding, **_kwargs: snapshot,
                )
                payload = build_snapshot(kind, result, now=current, environ=ENVIRONMENT)
                self.assertFalse(payload["quality"]["ready"])
                self.assertEqual(payload["analysis_state"], "DEGRADED")
                self.assertEqual(payload["bar_status"]["120m"], expected_bar)
                item = payload["result"]["analysis"]["items"][0]
                self.assertEqual(item["minute_context"]["input_ready"], expected_input_ready)
                self.assertFalse(item["minute_context"]["multi_cycle_confirm"])
                self.assertFalse(item["decision"]["auto_execute"])
                self.assertNotIn(item["decision"]["action"], {"BUY", "ADD"})
                self.assertIn("WAIT", item["next_trigger"]["action"])
                with TemporaryDirectory() as directory:
                    root = Path(directory)
                    latest = root / "latest"
                    latest.mkdir()
                    write_json_atomic(root / "current.json", payload)
                    write_json_atomic(latest / f"{kind}.json", payload)
                    manifest = build_manifest(
                        latest, root / "current.json", root / "manifest.json", now=current,
                    )
                    # A degraded warning can be published; it is not READY.
                    self.assertTrue(manifest["publishable"])

    def test_export_carries_minute_states_and_blocks_missing_or_invalid_evidence(self):
        for snapshot, expected, expected_context in (
            (_minute_snapshot(lines=[]), "MISSING", "missing"),
            (_minute_snapshot(base_minutes=1), "UNKNOWN", "invalid"),
        ):
            with self.subTest(expected=expected):
                result = self._run(loader=lambda _holding, **_kwargs: snapshot)
                payload = build_snapshot("trading", result, now=_at(14, 30), environ=ENVIRONMENT)
                self.assertEqual(payload["bar_status"]["120m"], expected)
                self.assertFalse(payload["quality"]["ready"])
                minute = payload["result"]["analysis"]["items"][0]["minute_context"]
                self.assertEqual(minute["bar_status"]["120m"], expected_context)
                self.assertFalse(minute["input_ready"])
                with TemporaryDirectory() as directory:
                    root = Path(directory)
                    latest = root / "latest"
                    latest.mkdir()
                    write_json_atomic(root / "current.json", payload)
                    write_json_atomic(latest / "trading.json", payload)
                    manifest = build_manifest(latest, root / "current.json", root / "manifest.json",
                                              now=_at(14, 30))
                    self.assertFalse(manifest["publishable"])


if __name__ == "__main__":
    unittest.main()
