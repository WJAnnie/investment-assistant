"""Tests for minute-to-structure bridge (tasks A, B, C).

Ensures closed minute lines are properly surfaced from minute context,
wired into the multi-cycle structure engine, and fail-closed invariants are preserved.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.domain.bars import BarStatus
from app.market.minute.context import MinuteSnapshot, load_minute_context
from app.market.minute.session import cn_trading_day
from app.portfolio.analysis import analyze_portfolio, _structure_evidence
from app.portfolio.calculator import build_portfolio_snapshot
from app.portfolio.models import AccountConfig, HoldingConfig, PortfolioConfig
from app.report.portfolio import format_portfolio_report


TZ = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 9, 14)
CYCLES = ("5m", "15m", "30m", "120m")


def _at(hour, minute=0, second=0):
    return datetime(2026, 9, 14, hour, minute, second, tzinfo=TZ)


def _holding(**changes):
    holding = HoldingConfig(
        code="600000",
        name="测试持仓",
        market="CN",
        instrument_type="stock",
        valuation_mode="exchange",
        cost_price=Decimal("9"),
        baseline_value=Decimal("100"),
        baseline_price=Decimal("10"),
        sector="financials",
        theme="bank",
        quantity=Decimal("10"),
    )
    return replace(holding, **changes)


def _config(holdings=None):
    return PortfolioConfig(
        schema_version=2,
        accounts=(
            AccountConfig(
                account_id="A",
                name="A账户",
                strategy="long_term_core",
                baseline_date=DAY,
                total_assets=Decimal("1000"),
                cash=Decimal("900"),
                holdings=tuple(holdings or (_holding(),)),
            ),
        ),
    )


def _minute_lines(until):
    result = []
    for opening in (_at(9, 30), _at(13)):
        for minutes in range(5, 121, 5):
            timestamp = opening + timedelta(minutes=minutes)
            if timestamp <= until:
                result.append(KLine(timestamp.isoformat(), 10.0, 12.0, 9.0, 11.0, 100.0))
    return result


def _minute_snapshot(now=None, **changes):
    now = now or _at(14, 30)
    snapshot = MinuteSnapshot(
        code="600000",
        market="CN",
        day=cn_trading_day(DAY, is_open=True, source="explicit calendar fixture"),
        lines=_minute_lines(now),
        source="offline minute fixture",
        fetched_at=now,
        base_minutes=5,
        timestamp_semantics="close",
    )
    return replace(snapshot, **changes)


def _daily_lines(count=35):
    return [
        KLine(
            (date(2026, 8, 1) + timedelta(days=index)).isoformat(),
            100.0 + index,
            101.0 + index,
            99.0 + index,
            100.0 + index,
            1000.0,
        )
        for index in range(count)
    ]


class MinuteContextExposureTests(unittest.TestCase):
    def test_full_day_closed_lines_at_15_00(self):
        now_15 = _at(15, 0)
        context = load_minute_context(
            _holding(),
            snapshot_loader=lambda _h, **_kw: _minute_snapshot(now_15),
            now=now_15,
        )
        self.assertEqual(context["status"], "ready")
        for cycle in CYCLES:
            cycle_data = context["cycles"][cycle]
            self.assertIn("closed_lines", cycle_data)
            closed_lines = cycle_data["closed_lines"]
            self.assertIsInstance(closed_lines, tuple)
            self.assertGreater(len(closed_lines), 0)
            for bar in closed_lines:
                self.assertIsInstance(bar, KLine)
            times = [datetime.fromisoformat(b.time) for b in closed_lines]
            self.assertEqual(times, sorted(times))
            self.assertEqual(len(times), len(set(times)))

        self.assertEqual(len(context["cycles"]["5m"]["closed_lines"]), 48)
        self.assertEqual(len(context["cycles"]["15m"]["closed_lines"]), 16)
        self.assertEqual(len(context["cycles"]["30m"]["closed_lines"]), 8)
        self.assertEqual(len(context["cycles"]["120m"]["closed_lines"]), 2)
        self.assertIs(context["multi_cycle_confirm"], False)

    def test_forming_bar_excluded_from_closed_lines_at_14_30(self):
        now_1430 = _at(14, 30)
        context = load_minute_context(
            _holding(),
            snapshot_loader=lambda _h, **_kw: _minute_snapshot(now_1430),
            now=now_1430,
        )
        self.assertEqual(context["status"], "ready")
        c120 = context["cycles"]["120m"]
        self.assertEqual(c120["current_status"], "forming")
        self.assertEqual(len(c120["closed_lines"]), 1)
        self.assertTrue(c120["closed_lines"][0].time.startswith("2026-09-14T11:30"))

        c30 = context["cycles"]["30m"]
        self.assertEqual(c30["current_status"], "closed")
        self.assertEqual(len(c30["closed_lines"]), 7)

        c15 = context["cycles"]["15m"]
        self.assertEqual(c15["current_status"], "closed")
        self.assertEqual(len(c15["closed_lines"]), 14)

        c5 = context["cycles"]["5m"]
        self.assertEqual(c5["current_status"], "closed")
        self.assertEqual(len(c5["closed_lines"]), 42)

        self.assertIs(context["multi_cycle_confirm"], False)

    def test_empty_or_missing_lines_yield_empty_closed_lines(self):
        now_1430 = _at(14, 30)
        context = load_minute_context(
            _holding(),
            snapshot_loader=lambda _h, **_kw: _minute_snapshot(now_1430, lines=[]),
            now=now_1430,
        )
        for cycle in CYCLES:
            self.assertEqual(context["cycles"][cycle]["closed_lines"], ())
        self.assertIs(context["multi_cycle_confirm"], False)

    def test_unavailable_path_keeps_empty_cycles_and_no_multi_cycle_confirm(self):
        context = load_minute_context(_holding(), snapshot_loader=None, now=_at(14, 30))
        self.assertEqual(context["status"], "unavailable")
        self.assertEqual(context["cycles"], {})
        self.assertIs(context["multi_cycle_confirm"], False)


class PortfolioMinuteStructureBridgeTests(unittest.TestCase):
    def test_structure_evidence_per_cycle_status_keys_and_values_at_14_30_and_15_00(self):
        expected_keys = {"weekly", "daily", "120m", "30m", "15m", "5m"}
        valid_statuses = {s.value for s in BarStatus}
        config = _config()
        snapshot = build_portfolio_snapshot(config, {})

        for now_dt in (_at(14, 30), _at(15, 0)):
            with self.subTest(time=now_dt.isoformat()):
                result = analyze_portfolio(
                    config,
                    snapshot,
                    history_loader=lambda _h, **_kw: _daily_lines(),
                    now=now_dt,
                    minute_snapshot_loader=lambda _h, **_kw: _minute_snapshot(now_dt),
                )
                item = result["items"][0]
                self.assertEqual(item["status"], "ready")
                self.assertIsNotNone(item.get("structure"))
                structure = item["structure"]
                per_cycle = structure["per_cycle_status"]
                self.assertEqual(set(per_cycle.keys()), expected_keys)
                for tf_name, status_val in per_cycle.items():
                    self.assertIn(status_val, valid_statuses)

                self.assertIs(structure["ready"], False)
                self.assertNotEqual(structure["outcome"], "CONFIRMED")
                self.assertNotIn(item["decision"]["action"], {"BUY", "ADD"})
                self.assertIs(item["decision"]["auto_execute"], False)
                self.assertEqual(result["data_limits"]["structure"], "dated_and_minute_cycles")

                if now_dt == _at(14, 30):
                    self.assertEqual(per_cycle["120m"], "forming")
                    self.assertEqual(per_cycle["30m"], "closed")
                    self.assertEqual(per_cycle["15m"], "closed")
                    self.assertEqual(per_cycle["5m"], "closed")
                elif now_dt == _at(15, 0):
                    self.assertEqual(per_cycle["120m"], "closed")
                    self.assertEqual(per_cycle["30m"], "closed")
                    self.assertEqual(per_cycle["15m"], "closed")
                    self.assertEqual(per_cycle["5m"], "closed")

    def test_five_minute_only_ready_never_confirmed(self):
        config = _config()
        snapshot = build_portfolio_snapshot(config, {})
        now_dt = _at(15, 0)

        def five_m_only_loader(_h, **_kw):
            line = KLine(_at(9, 35).isoformat(), 10.0, 11.0, 9.5, 10.5, 100.0)
            return _minute_snapshot(now_dt, lines=[line])

        result = analyze_portfolio(
            config,
            snapshot,
            history_loader=lambda _h, **_kw: _daily_lines(),
            now=now_dt,
            minute_snapshot_loader=five_m_only_loader,
        )
        item = result["items"][0]
        structure = item["structure"]
        self.assertIs(structure["ready"], False)
        self.assertNotEqual(structure["outcome"], "CONFIRMED")

    def test_no_minute_snapshot_loader_keeps_dated_cycles_only_contract(self):
        config = _config()
        snapshot = build_portfolio_snapshot(config, {})
        result = analyze_portfolio(
            config,
            snapshot,
            history_loader=lambda _h, **_kw: _daily_lines(),
            now=_at(15, 0),
            minute_snapshot_loader=None,
        )
        item = result["items"][0]
        structure = item["structure"]
        self.assertIs(structure["ready"], False)
        for tf in ("120m", "30m", "15m", "5m"):
            self.assertEqual(structure["per_cycle_status"][tf], "missing")
        self.assertEqual(result["data_limits"]["structure"], "dated_cycles_only")

    def test_loader_exception_isolated_and_no_leak_in_report(self):
        config = _config()
        snapshot = build_portfolio_snapshot(config, {})
        secret = "password=super_secret_db_pass_12345"
        mock_loader = Mock(side_effect=RuntimeError(secret))

        result = analyze_portfolio(
            config,
            snapshot,
            history_loader=lambda _h, **_kw: _daily_lines(),
            now=_at(15, 0),
            minute_snapshot_loader=mock_loader,
        )
        item = result["items"][0]
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["minute_context"]["status"], "unavailable")
        self.assertEqual(result["data_limits"]["structure"], "dated_cycles_only")

        report = format_portfolio_report(snapshot, "trading", _at(15, 0), analysis=result)
        self.assertNotIn(secret, report)
        self.assertNotIn("super_secret", report)
        self.assertNotIn("password=", repr(result))


class MinuteStructureExceptionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = _at(15, 0)
        self.lines = _daily_lines()

    def test_minute_context_none(self):
        res = _structure_evidence(
            "600000", "CN", "daily", self.lines, self.cutoff, True, minute_context=None
        )
        self.assertIsNotNone(res)
        for tf in ("120m", "30m", "15m", "5m"):
            self.assertEqual(res.per_cycle_status[tf], "missing")

    def test_minute_context_missing_cycles(self):
        res = _structure_evidence(
            "600000", "CN", "daily", self.lines, self.cutoff, True, minute_context={}
        )
        self.assertIsNotNone(res)
        for tf in ("120m", "30m", "15m", "5m"):
            self.assertEqual(res.per_cycle_status[tf], "missing")

    def test_cycle_missing_closed_lines(self):
        minute_ctx = {
            "cycles": {
                "120m": {"status": "closed"},
                "30m": {"status": "closed"},
            }
        }
        res = _structure_evidence(
            "600000", "CN", "daily", self.lines, self.cutoff, True, minute_context=minute_ctx
        )
        self.assertIsNotNone(res)
        self.assertEqual(res.per_cycle_status["120m"], "missing")
        self.assertEqual(res.per_cycle_status["30m"], "missing")

    def test_cycle_unrecognized_status_string(self):
        bar = self.lines[0]
        minute_ctx = {
            "cycles": {
                "120m": {"status": "totally_invalid_status_xyz", "closed_lines": [bar]},
            }
        }
        res = _structure_evidence(
            "600000", "CN", "daily", self.lines, self.cutoff, True, minute_context=minute_ctx
        )
        self.assertIsNotNone(res)
        self.assertEqual(res.per_cycle_status["120m"], "missing")

    def test_cycle_closed_lines_not_list_or_tuple(self):
        minute_ctx = {
            "cycles": {
                "120m": {"status": "closed", "closed_lines": "not_a_list"},
            }
        }
        res = _structure_evidence(
            "600000", "CN", "daily", self.lines, self.cutoff, True, minute_context=minute_ctx
        )
        self.assertIsNotNone(res)
        self.assertEqual(res.per_cycle_status["120m"], "missing")

    def test_cycle_closed_lines_contains_invalid_item_types(self):
        minute_ctx = {
            "cycles": {
                "120m": {"status": "closed", "closed_lines": [123, "abc"]},
            }
        }
        res = _structure_evidence(
            "600000", "CN", "daily", self.lines, self.cutoff, True, minute_context=minute_ctx
        )
        self.assertIsNotNone(res)
        self.assertEqual(res.per_cycle_status["120m"], "missing")

    def test_cycle_info_is_not_mapping(self):
        minute_ctx = {
            "cycles": {
                "120m": "string_instead_of_dict",
            }
        }
        res = _structure_evidence(
            "600000", "CN", "daily", self.lines, self.cutoff, True, minute_context=minute_ctx
        )
        self.assertIsNotNone(res)
        self.assertEqual(res.per_cycle_status["120m"], "missing")

    def test_cycles_key_is_not_mapping(self):
        minute_ctx = {"cycles": ["not", "a", "mapping"]}
        res = _structure_evidence(
            "600000", "CN", "daily", self.lines, self.cutoff, True, minute_context=minute_ctx
        )
        self.assertIsNotNone(res)
        for tf in ("120m", "30m", "15m", "5m"):
            self.assertEqual(res.per_cycle_status[tf], "missing")


if __name__ == "__main__":
    unittest.main()
