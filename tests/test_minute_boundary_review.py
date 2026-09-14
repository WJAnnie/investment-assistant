"""Independent adversarial checks for the executor's minute implementation."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest

from app.domain.bars import BarStatus
from app.domain.timeframe import Timeframe
from app.market.minute.resample import current_bar, resample_minutes
from app.market.minute.session import SessionWindow, TradingDay, cn_trading_day
from tests.test_minute_context import DAY, _at, _minute_lines


class MinuteBoundaryReviewTests(unittest.TestCase):
    def setUp(self):
        self.day = cn_trading_day(DAY, is_open=True, source="independent calendar fixture")

    def test_selection_cannot_relabel_future_closed_evidence_as_current(self):
        bars = resample_minutes(_minute_lines(_at(11, 30)), day=self.day, now=_at(11, 30))
        with self.assertRaises(ValueError):
            current_bar(bars[Timeframe.MIN_120], now=_at(9, 30))

    def test_adjacent_session_open_takes_precedence_over_previous_session_close(self):
        day = TradingDay("CN", DAY, (
            SessionWindow("first", _at(9, 30), _at(10, 30)),
            SessionWindow("second", _at(10, 30), _at(11, 30)),
        ), "explicit adjacent-session fixture")
        bars = resample_minutes(_minute_lines(_at(10, 30)), day=day, now=_at(10, 30))
        selected = current_bar(bars[Timeframe.MIN_30], now=_at(10, 30))
        self.assertEqual(selected.session, "second")
        self.assertEqual(selected.status, BarStatus.FORMING)
        self.assertIsNone(selected.line)

    def test_whole_day_edges_and_seconds_never_expose_incomplete_ohlc(self):
        edges = (_at(9, 30), _at(10), _at(11, 30), _at(12), _at(13), _at(14, 30), _at(15))
        for edge in edges:
            for delta in (-1, 0, 1):
                now = edge + timedelta(seconds=delta)
                bars = resample_minutes(_minute_lines(now), day=self.day, now=now)
                for cycle, minutes in ((Timeframe.MIN_5, 5), (Timeframe.MIN_15, 15),
                                       (Timeframe.MIN_30, 30), (Timeframe.MIN_120, 120)):
                    with self.subTest(now=now, cycle=cycle):
                        selected = current_bar(bars[cycle], now=now)
                        if now < _at(9, 30):
                            self.assertIsNone(selected)
                        else:
                            opening = _at(9, 30) if now < _at(13) else _at(13)
                            elapsed = min((now - opening).total_seconds(), 120 * 60)
                            index = max(0, int((elapsed - 1) // (minutes * 60)))
                            if elapsed % (minutes * 60) != 0:
                                index = int(elapsed // (minutes * 60))
                            expected_close = opening + timedelta(minutes=(index + 1) * minutes)
                            self.assertEqual(selected.expected_close, expected_close)
                            expected_status = BarStatus.CLOSED if now >= expected_close else BarStatus.FORMING
                            self.assertEqual(selected.status, expected_status)
                        for bar in bars[cycle]:
                            if bar.status is BarStatus.CLOSED:
                                self.assertIsNotNone(bar.line)
                                self.assertLessEqual(bar.expected_close, now)
                                self.assertEqual(bar.observed, bar.expected)
                            else:
                                self.assertIsNone(bar.line)

    def test_removing_any_single_base_bar_leaves_one_missing_bucket_per_cycle(self):
        lines = _minute_lines(_at(15))
        for missing_index in range(48):
            with self.subTest(missing_index=missing_index):
                result = resample_minutes(lines[:missing_index] + lines[missing_index + 1:],
                                          day=self.day, now=_at(15))
                for bars in result.values():
                    self.assertEqual(sum(bar.status is BarStatus.MISSING for bar in bars), 1)
                    self.assertTrue(all(bar.line is None for bar in bars if bar.status is BarStatus.MISSING))

    def test_all_ohlcv_fields_reject_nonfinite_boolean_and_malformed_values(self):
        line = _minute_lines(_at(9, 35))[0]
        for field in ("open", "high", "low", "close", "volume"):
            for value in (True, False, float("nan"), float("inf"), float("-inf"),
                          Decimal("sNaN"), "10", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises((TypeError, ValueError, ArithmeticError)):
                        resample_minutes([replace(line, **{field: value})], day=self.day, now=_at(9, 35))

    def test_equivalent_utc_and_local_timestamps_are_duplicate_not_distinct_bars(self):
        line = _minute_lines(_at(9, 35))[0]
        duplicate = replace(line, time="2026-09-14T01:35:00Z")
        with self.assertRaises(ValueError):
            resample_minutes([line, duplicate], day=self.day, now=_at(9, 35))

    def test_closed_day_cannot_accept_source_bars(self):
        day = cn_trading_day(DAY, is_open=False, source="explicit closure fixture")
        with self.assertRaises(ValueError):
            resample_minutes(_minute_lines(_at(9, 35)), day=day, now=_at(9, 35))

    def test_input_is_not_modified_and_closed_output_does_not_alias_source(self):
        lines = _minute_lines(_at(9, 45))
        before = [replace(line) for line in lines]
        result = resample_minutes(lines, day=self.day, now=_at(9, 45))
        result[Timeframe.MIN_5][0].line.close = 999
        self.assertEqual(lines, before)


if __name__ == "__main__":
    unittest.main()
