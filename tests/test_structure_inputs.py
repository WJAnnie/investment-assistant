"""Unit tests for app.portfolio.structure_inputs.

Follows the repository's standard unittest style (consistent with
test_portfolio_structure.py and test_multi_cycle_confirm.py).
"""

import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.chan.multi_cycle_confirm import ConfirmOutcome
from app.domain.bars import BarStatus
from app.domain.timeframe import Timeframe
from app.portfolio.structure import CycleInput, build_structure_evidence
from app.portfolio.structure_inputs import (
    DATED_TIMEFRAMES,
    MARKET_CLOSES,
    MINUTE_TIMEFRAMES,
    assemble_cycles,
    cycle_from_dated_lines,
    cycle_from_minute_bars,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
HONG_KONG = ZoneInfo("Asia/Hong_Kong")


def _make_line(
    bar_time: object,
    open_: float = 10.0,
    high: float = 11.0,
    low: float = 9.5,
    close: float = 10.5,
    volume: float = 1000.0,
) -> KLine:
    return KLine(
        time=bar_time,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


class TestCycleFromDatedLines(unittest.TestCase):
    """Tests for cycle_from_dated_lines (DAILY / WEEKLY restamping & validation)."""

    def test_cn_daily_restamp_to_1500(self):
        lines = [
            _make_line("2026-09-15", 10.0, 11.0, 9.0, 10.5, 100.0),
            _make_line("2026-09-16", 10.5, 11.5, 10.0, 11.0, 200.0),
        ]
        cycle = cycle_from_dated_lines(
            Timeframe.DAILY,
            lines,
            market="CN",
            source="test.cn.daily",
        )

        self.assertIsInstance(cycle, CycleInput)
        self.assertEqual(cycle.status, BarStatus.CLOSED)
        self.assertIsInstance(cycle.bars, tuple)
        self.assertEqual(len(cycle.bars), 2)
        self.assertEqual(cycle.bars[0].time, "2026-09-15T15:00:00+08:00")
        self.assertEqual(cycle.bars[1].time, "2026-09-16T15:00:00+08:00")
        self.assertEqual(cycle.source, "test.cn.daily")

    def test_hk_daily_restamp_to_1610(self):
        lines = [
            _make_line("2026-09-15", 20.0, 21.0, 19.5, 20.5, 500.0),
        ]
        cycle = cycle_from_dated_lines(
            Timeframe.DAILY,
            lines,
            market="HK",
            source="test.hk.daily",
        )

        self.assertEqual(cycle.bars[0].time, "2026-09-15T16:10:00+08:00")

    def test_weekly_restamp_cn_and_hk(self):
        cn_lines = [
            _make_line("2026-09-11", 10.0, 11.0, 9.0, 10.5, 1000.0),
            _make_line("2026-09-18", 10.5, 12.0, 10.0, 11.8, 1200.0),
        ]
        cn_cycle = cycle_from_dated_lines(
            Timeframe.WEEKLY,
            cn_lines,
            market="CN",
            source="test.cn.weekly",
        )
        self.assertEqual(cn_cycle.bars[0].time, "2026-09-11T15:00:00+08:00")
        self.assertEqual(cn_cycle.bars[1].time, "2026-09-18T15:00:00+08:00")

        hk_lines = [
            _make_line("2026-09-11", 20.0, 22.0, 19.0, 21.5, 2000.0),
        ]
        hk_cycle = cycle_from_dated_lines(
            Timeframe.WEEKLY,
            hk_lines,
            market="HK",
            source="test.hk.weekly",
        )
        self.assertEqual(hk_cycle.bars[0].time, "2026-09-11T16:10:00+08:00")

    def test_ohlcv_verbatim_copy(self):
        line = _make_line("2026-09-15", 10.125, 12.345, 9.876, 11.432, 987654.3)
        cycle = cycle_from_dated_lines(
            Timeframe.DAILY,
            [line],
            market="CN",
            source="test.ohlcv",
        )

        self.assertEqual(cycle.status, BarStatus.CLOSED)
        self.assertIsInstance(cycle.bars, tuple)
        bar = cycle.bars[0]
        self.assertEqual(bar.open, 10.125)
        self.assertEqual(bar.high, 12.345)
        self.assertEqual(bar.low, 9.876)
        self.assertEqual(bar.close, 11.432)
        self.assertEqual(bar.volume, 987654.3)

    def test_supported_date_input_types(self):
        lines = [
            _make_line("2026-09-10"),  # YYYY-MM-DD
            _make_line("2026/09/11"),  # YYYY/MM/DD
            _make_line("2026-09-12T10:00:00"),  # ISO string naive
            _make_line("2026-09-13T10:00:00+08:00"),  # ISO string aware
            _make_line(date(2026, 9, 14)),  # date instance
            _make_line(datetime(2026, 9, 15, 12, 0, tzinfo=SHANGHAI)),  # aware datetime
            _make_line(datetime(2026, 9, 16, 9, 30)),  # naive datetime -> .date()
        ]
        cycle = cycle_from_dated_lines(
            Timeframe.DAILY,
            lines,
            market="CN",
            source="test.dates",
        )

        expected_dates = [
            "2026-09-10",
            "2026-09-11",
            "2026-09-12",
            "2026-09-13",
            "2026-09-14",
            "2026-09-15",
            "2026-09-16",
        ]
        self.assertEqual(len(cycle.bars), len(expected_dates))
        for bar, exp_date in zip(cycle.bars, expected_dates):
            self.assertEqual(bar.time, f"{exp_date}T15:00:00+08:00")

    def test_boundary_empty_lines(self):
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.DAILY, [], market="CN", source="test")
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.DAILY, (), market="CN", source="test")

    def test_boundary_non_strictly_increasing_dates(self):
        # duplicate dates
        dup = [_make_line("2026-09-15"), _make_line("2026-09-15")]
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.DAILY, dup, market="CN", source="test")

        # descending dates
        desc = [_make_line("2026-09-16"), _make_line("2026-09-15")]
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.DAILY, desc, market="CN", source="test")

    def test_boundary_lines_type(self):
        # generator
        gen = (_make_line("2026-09-15") for _ in range(1))
        with self.assertRaises(TypeError):
            cycle_from_dated_lines(Timeframe.DAILY, gen, market="CN", source="test")

        # string
        with self.assertRaises(TypeError):
            cycle_from_dated_lines(Timeframe.DAILY, "not_lines", market="CN", source="test")

        # non-KLine item
        with self.assertRaises(TypeError):
            cycle_from_dated_lines(Timeframe.DAILY, [_make_line("2026-09-15"), "invalid"], market="CN", source="test")

    def test_boundary_timeframe_validation(self):
        lines = [_make_line("2026-09-15")]
        # non-Timeframe
        with self.assertRaises(TypeError):
            cycle_from_dated_lines("daily", lines, market="CN", source="test")

        # unsupported timeframe (e.g. MIN_15, MIN_120)
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.MIN_15, lines, market="CN", source="test")
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.MIN_120, lines, market="CN", source="test")

    def test_boundary_market_validation(self):
        lines = [_make_line("2026-09-15")]
        # non-string market
        with self.assertRaises(TypeError):
            cycle_from_dated_lines(Timeframe.DAILY, lines, market=123, source="test")
        with self.assertRaises(TypeError):
            cycle_from_dated_lines(Timeframe.DAILY, lines, market=None, source="test")

        # unsupported market
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.DAILY, lines, market="US", source="test")
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.DAILY, lines, market="", source="test")

    def test_boundary_source_validation(self):
        lines = [_make_line("2026-09-15")]
        # non-string source
        with self.assertRaises(TypeError):
            cycle_from_dated_lines(Timeframe.DAILY, lines, market="CN", source=123)
        with self.assertRaises(TypeError):
            cycle_from_dated_lines(Timeframe.DAILY, lines, market="CN", source=None)

        # blank source
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.DAILY, lines, market="CN", source="")
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.DAILY, lines, market="CN", source="   ")

    def test_boundary_invalid_bar_time(self):
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.DAILY, [_make_line("")], market="CN", source="test")
        with self.assertRaises(ValueError):
            cycle_from_dated_lines(Timeframe.DAILY, [_make_line("invalid-date-format")], market="CN", source="test")
        with self.assertRaises(TypeError):
            cycle_from_dated_lines(Timeframe.DAILY, [_make_line(123456)], market="CN", source="test")

    def test_output_time_not_later_than_market_close(self):
        lines = [_make_line("2026-09-15")]
        cn_cycle = cycle_from_dated_lines(Timeframe.DAILY, lines, market="CN", source="test")
        hk_cycle = cycle_from_dated_lines(Timeframe.DAILY, lines, market="HK", source="test")

        cn_dt = datetime.fromisoformat(cn_cycle.bars[0].time)
        self.assertEqual(cn_dt.time(), time(15, 0))
        self.assertLessEqual(cn_dt.time(), time(15, 0))

        hk_dt = datetime.fromisoformat(hk_cycle.bars[0].time)
        self.assertEqual(hk_dt.time(), time(16, 10))
        self.assertLessEqual(hk_dt.time(), time(16, 10))

    def test_integration_with_build_structure_evidence(self):
        lines = [
            _make_line(f"2026-09-{day:02d}")
            for day in range(10, 15)
        ]
        cycle = cycle_from_dated_lines(Timeframe.DAILY, lines, market="CN", source="test.daily")
        cutoff = datetime(2026, 9, 15, 15, 0, tzinfo=SHANGHAI)

        result = build_structure_evidence(
            code="000001",
            market="CN",
            cycles={Timeframe.DAILY: cycle},
            cutoff=cutoff,
        )
        self.assertEqual(result.outcome, ConfirmOutcome.WAIT)
        self.assertEqual(result.reason_code, "STRUCTURE_DATA_MISSING")
        self.assertEqual(result.per_cycle_status["daily"], "closed")


class TestCycleFromMinuteBars(unittest.TestCase):
    """Tests for cycle_from_minute_bars."""

    def test_minute_bars_normal_and_all_statuses(self):
        for st in BarStatus:
            bars = (
                _make_line("2026-09-16T14:50:00+08:00"),
                _make_line("2026-09-16T14:55:00+08:00"),
            )
            cycle = cycle_from_minute_bars(
                Timeframe.MIN_5,
                bars,
                status=st,
                source="test.m5",
            )
            self.assertIsInstance(cycle, CycleInput)
            self.assertEqual(cycle.status, st)
            self.assertIsInstance(cycle.bars, tuple)
            self.assertEqual(len(cycle.bars), 2)
            # Timestamps must be preserved as-is, never restamped
            self.assertEqual(cycle.bars[0].time, "2026-09-16T14:50:00+08:00")
            self.assertEqual(cycle.bars[1].time, "2026-09-16T14:55:00+08:00")
            self.assertEqual(cycle.source, "test.m5")

    def test_minute_bars_list_input_converted_to_tuple(self):
        bars = [
            _make_line("2026-09-16T14:00:00+08:00"),
        ]
        cycle = cycle_from_minute_bars(
            Timeframe.MIN_15,
            bars,
            status=BarStatus.CLOSED,
            source="test.m15",
        )
        self.assertIsInstance(cycle.bars, tuple)
        self.assertEqual(len(cycle.bars), 1)

    def test_boundary_empty_bars(self):
        with self.assertRaises(ValueError):
            cycle_from_minute_bars(Timeframe.MIN_5, [], status=BarStatus.CLOSED, source="test")
        with self.assertRaises(ValueError):
            cycle_from_minute_bars(Timeframe.MIN_5, (), status=BarStatus.CLOSED, source="test")

    def test_boundary_non_kline_element(self):
        with self.assertRaises(TypeError):
            cycle_from_minute_bars(
                Timeframe.MIN_5,
                [_make_line("2026-09-16T14:55:00+08:00"), "invalid"],
                status=BarStatus.CLOSED,
                source="test",
            )

    def test_boundary_bars_container_type(self):
        with self.assertRaises(TypeError):
            cycle_from_minute_bars(Timeframe.MIN_5, "invalid", status=BarStatus.CLOSED, source="test")
        with self.assertRaises(TypeError):
            cycle_from_minute_bars(Timeframe.MIN_5, 12345, status=BarStatus.CLOSED, source="test")

    def test_boundary_status_validation(self):
        bars = [_make_line("2026-09-16T14:55:00+08:00")]
        with self.assertRaises(TypeError):
            cycle_from_minute_bars(Timeframe.MIN_5, bars, status="closed", source="test")
        with self.assertRaises(TypeError):
            cycle_from_minute_bars(Timeframe.MIN_5, bars, status=None, source="test")

    def test_boundary_timeframe_validation(self):
        bars = [_make_line("2026-09-16T14:55:00+08:00")]
        # non-Timeframe
        with self.assertRaises(TypeError):
            cycle_from_minute_bars("5m", bars, status=BarStatus.CLOSED, source="test")

        # DAILY / WEEKLY not allowed for minute bars
        with self.assertRaises(ValueError):
            cycle_from_minute_bars(Timeframe.DAILY, bars, status=BarStatus.CLOSED, source="test")
        with self.assertRaises(ValueError):
            cycle_from_minute_bars(Timeframe.WEEKLY, bars, status=BarStatus.CLOSED, source="test")

    def test_boundary_source_validation(self):
        bars = [_make_line("2026-09-16T14:55:00+08:00")]
        with self.assertRaises(TypeError):
            cycle_from_minute_bars(Timeframe.MIN_5, bars, status=BarStatus.CLOSED, source=123)
        with self.assertRaises(ValueError):
            cycle_from_minute_bars(Timeframe.MIN_5, bars, status=BarStatus.CLOSED, source="")
        with self.assertRaises(ValueError):
            cycle_from_minute_bars(Timeframe.MIN_5, bars, status=BarStatus.CLOSED, source="   ")


class TestAssembleCycles(unittest.TestCase):
    """Tests for assemble_cycles."""

    def test_mixed_none_and_valid_pairs(self):
        c_daily = CycleInput(bars=(_make_line("2026-09-15T15:00:00+08:00"),), status=BarStatus.CLOSED, source="d")
        c_m15 = CycleInput(bars=(_make_line("2026-09-15T15:00:00+08:00"),), status=BarStatus.FORMING, source="15m")

        res = assemble_cycles(
            None,
            (Timeframe.DAILY, c_daily),
            None,
            (Timeframe.MIN_15, c_m15),
            None,
        )
        self.assertEqual(len(res), 2)
        self.assertEqual(res[Timeframe.DAILY], c_daily)
        self.assertEqual(res[Timeframe.MIN_15], c_m15)

    def test_duplicate_timeframe_raises_value_error(self):
        c1 = CycleInput(bars=(_make_line("2026-09-15T15:00:00+08:00"),), status=BarStatus.CLOSED, source="d1")
        c2 = CycleInput(bars=(_make_line("2026-09-16T15:00:00+08:00"),), status=BarStatus.CLOSED, source="d2")
        with self.assertRaises(ValueError):
            assemble_cycles((Timeframe.DAILY, c1), (Timeframe.DAILY, c2))

    def test_element_not_two_tuple_raises_type_error(self):
        c = CycleInput(bars=(_make_line("2026-09-15T15:00:00+08:00"),), status=BarStatus.CLOSED, source="d")
        with self.assertRaises(TypeError):
            assemble_cycles((Timeframe.DAILY,))
        with self.assertRaises(TypeError):
            assemble_cycles((Timeframe.DAILY, c, "extra"))
        with self.assertRaises(TypeError):
            assemble_cycles(c)
        with self.assertRaises(TypeError):
            assemble_cycles("not_a_tuple")

    def test_invalid_pair_key_or_value_raises_type_error(self):
        c = CycleInput(bars=(_make_line("2026-09-15T15:00:00+08:00"),), status=BarStatus.CLOSED, source="d")
        # key not Timeframe
        with self.assertRaises(TypeError):
            assemble_cycles(("daily", c))

        # value not CycleInput
        with self.assertRaises(TypeError):
            assemble_cycles((Timeframe.DAILY, "not_a_cycle_input"))

    def test_empty_call_returns_empty_dict(self):
        res1 = assemble_cycles()
        self.assertIsInstance(res1, dict)
        self.assertEqual(res1, {})

        res2 = assemble_cycles(None, None)
        self.assertIsInstance(res2, dict)
        self.assertEqual(res2, {})

    def test_no_fabrication_of_missing_cycles(self):
        c = CycleInput(bars=(_make_line("2026-09-15T15:00:00+08:00"),), status=BarStatus.CLOSED, source="d")
        res = assemble_cycles((Timeframe.DAILY, c))
        self.assertEqual(len(res), 1)
        self.assertIn(Timeframe.DAILY, res)
        self.assertNotIn(Timeframe.WEEKLY, res)
        self.assertNotIn(Timeframe.MIN_120, res)


class TestImmutabilityAndDefense(unittest.TestCase):
    """Defensive tests for CycleInput immutability."""

    def test_cycle_input_frozen(self):
        cycle = CycleInput(
            bars=(_make_line("2026-09-15T15:00:00+08:00"),),
            status=BarStatus.CLOSED,
            source="test.defense",
        )
        with self.assertRaises(FrozenInstanceError):
            cycle.status = BarStatus.FORMING
        with self.assertRaises(FrozenInstanceError):
            cycle.source = "new.source"
        with self.assertRaises(FrozenInstanceError):
            cycle.bars = ()


class TestNoHeavyDependencies(unittest.TestCase):
    """Ensure app.portfolio.structure_inputs does not import heavy dependencies."""

    def test_no_heavy_dependencies_in_isolated_process(self):
        code = (
            "import sys, app.portfolio.structure_inputs; "
            "heavy = [m for m in ('pandas', 'numpy', 'requests', 'yaml') if m in sys.modules]; "
            "assert not heavy, f'Heavy modules imported: {heavy}'"
        )
        res = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"Subprocess failed: {res.stderr}")


if __name__ == "__main__":
    unittest.main()
