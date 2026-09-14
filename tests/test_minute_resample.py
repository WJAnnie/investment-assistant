import math
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.domain import BarStatus, Timeframe
from app.market.minute.resample import current_bar, resample_minutes
from app.market.minute.session import SessionWindow, TradingDay, cn_trading_day


SHANGHAI = ZoneInfo("Asia/Shanghai")


def shanghai_at(value: str) -> datetime:
    return datetime.fromisoformat(f"2026-09-14T{value}:00+08:00")


def line_at(value: str, open_=10.0, high=11.0, low=9.0, close=10.5, volume=100.0):
    return KLine(shanghai_at(value).isoformat(), open_, high, low, close, volume)


def morning_lines(until: str = "11:30"):
    close = shanghai_at("09:35")
    end = shanghai_at(until)
    lines = []
    value = 1.0
    while close <= end:
        lines.append(KLine(close.isoformat(), value, value + 0.5, value - 0.25, value + 0.25, value * 10))
        value += 1.0
        close += timedelta(minutes=5)
    return lines


def afternoon_lines(until: str = "15:00", *, start_value: float = 25.0):
    close = shanghai_at("13:05")
    end = shanghai_at(until)
    lines = []
    value = start_value
    while close <= end:
        lines.append(KLine(close.isoformat(), value, value + 0.5, value - 0.25, value + 0.25, value * 10))
        value += 1.0
        close += timedelta(minutes=5)
    return lines


def open_day():
    return cn_trading_day(date(2026, 9, 14), is_open=True, source="official-calendar")


class MinuteSessionTests(unittest.TestCase):
    def test_cn_trading_day_requires_explicit_bool_source_and_exact_date(self):
        with self.assertRaises(TypeError):
            cn_trading_day(datetime(2026, 9, 14), is_open=True, source="official-calendar")
        with self.assertRaises(TypeError):
            cn_trading_day(date(2026, 9, 14), is_open=1, source="official-calendar")
        with self.assertRaises(ValueError):
            cn_trading_day(date(2026, 9, 14), is_open=True, source="")
        with self.assertRaises(TypeError):
            cn_trading_day(date(2026, 9, 14), is_open=True, source="official-calendar", half_day=0)

    def test_cn_trading_day_builds_full_half_and_closed_cn_sessions(self):
        full = open_day()
        self.assertEqual(full.market, "CN")
        self.assertEqual([session.name for session in full.sessions], ["morning", "afternoon"])
        self.assertEqual((full.sessions[0].opens_at.time(), full.sessions[0].closes_at.time()), (shanghai_at("09:30").time(), shanghai_at("11:30").time()))
        self.assertEqual((full.sessions[1].opens_at.time(), full.sessions[1].closes_at.time()), (shanghai_at("13:00").time(), shanghai_at("15:00").time()))

        half = cn_trading_day(date(2026, 9, 14), is_open=True, source="official-calendar", half_day=True)
        self.assertEqual([session.name for session in half.sessions], ["morning"])

        closed = cn_trading_day(date(2026, 9, 14), is_open=False, source="official-calendar")
        self.assertEqual(closed.sessions, ())

    def test_session_and_trading_day_validate_awareness_order_overlap_and_grid(self):
        naive_open = datetime(2026, 9, 14, 9, 30)
        aware_close = shanghai_at("11:30")
        with self.assertRaises(ValueError):
            SessionWindow("morning", naive_open, aware_close)
        with self.assertRaises(ValueError):
            SessionWindow("bad", shanghai_at("10:00"), shanghai_at("09:30"))
        with self.assertRaises(ValueError):
            SessionWindow("bad", shanghai_at("09:30"), shanghai_at("09:37"))
        with self.assertRaises(ValueError):
            SessionWindow("bad", shanghai_at("09:31"), shanghai_at("09:36"))

        morning = SessionWindow("morning", shanghai_at("09:30"), shanghai_at("11:30"))
        overlap = SessionWindow("overlap", shanghai_at("11:25"), shanghai_at("11:35"))
        with self.assertRaises(ValueError):
            TradingDay("CN", date(2026, 9, 14), (morning, overlap), "official-calendar")
        with self.assertRaises(ValueError):
            TradingDay("HK", date(2026, 9, 14), (), "official-calendar")


class MinuteResampleTests(unittest.TestCase):
    def test_resamples_complete_morning_bars_with_close_right_timestamps(self):
        lines = morning_lines()

        result = resample_minutes(lines, day=open_day(), now=shanghai_at("11:30"))

        self.assertEqual(len(result[Timeframe.MIN_5]), 48)
        first_15 = result[Timeframe.MIN_15][0]
        self.assertEqual(first_15.status, BarStatus.CLOSED)
        self.assertEqual((first_15.opens_at.time(), first_15.expected_close.time()), (shanghai_at("09:30").time(), shanghai_at("09:45").time()))
        self.assertEqual(first_15.observed, 3)
        self.assertEqual(first_15.expected, 3)
        self.assertEqual(first_15.line, KLine(shanghai_at("09:45").isoformat(), 1.0, 3.5, 0.75, 3.25, 60.0))

    def test_accepts_utc_and_iso_offset_times_after_shanghai_normalization(self):
        lines = [
            KLine("2026-09-14T01:35:00+00:00", 1, 2, 0.5, 1.5, 10),
            KLine("2026-09-14T01:40:00+00:00", 1.5, 2.5, 1.0, 2.0, 20),
            KLine("2026-09-14T01:45:00+00:00", 2.0, 3.0, 1.5, 2.5, 30),
        ]

        first_15 = resample_minutes(lines, day=open_day(), now=shanghai_at("09:45"))[Timeframe.MIN_15][0]

        self.assertEqual(first_15.status, BarStatus.CLOSED)
        self.assertEqual(first_15.line.time, shanghai_at("09:45").isoformat())

    def test_boundaries_and_current_bar_selection(self):
        bars_0929 = resample_minutes([], day=open_day(), now=shanghai_at("09:29"))[Timeframe.MIN_120]
        self.assertEqual(current_bar(bars_0929, now=shanghai_at("09:29")), None)

        bars_0930 = resample_minutes([], day=open_day(), now=shanghai_at("09:30"))[Timeframe.MIN_120]
        selected_0930 = current_bar(bars_0930, now=shanghai_at("09:30"))
        self.assertEqual(selected_0930.opens_at, shanghai_at("09:30"))
        self.assertEqual(selected_0930.status, BarStatus.FORMING)

        bars_1000 = resample_minutes(morning_lines("10:00"), day=open_day(), now=shanghai_at("10:00"))[Timeframe.MIN_120]
        selected_1000 = current_bar(bars_1000, now=shanghai_at("10:00"))
        self.assertEqual(selected_1000.expected_close, shanghai_at("11:30"))
        self.assertEqual(selected_1000.status, BarStatus.FORMING)

        bars_1130 = resample_minutes(morning_lines(), day=open_day(), now=shanghai_at("11:30"))[Timeframe.MIN_120]
        self.assertEqual(current_bar(bars_1130, now=shanghai_at("11:30")).status, BarStatus.CLOSED)
        self.assertEqual(current_bar(bars_1130, now=shanghai_at("11:30") + timedelta(seconds=1)).status, BarStatus.CLOSED)

        with self.assertRaises(ValueError):
            current_bar(bars_1130, now=shanghai_at("09:30"))

        bars_1300 = resample_minutes(morning_lines(), day=open_day(), now=shanghai_at("13:00"))[Timeframe.MIN_120]
        self.assertEqual(current_bar(bars_1300, now=shanghai_at("13:00")).opens_at, shanghai_at("13:00"))

        bars_1430 = resample_minutes(morning_lines() + afternoon_lines("14:30"), day=open_day(), now=shanghai_at("14:30"))[Timeframe.MIN_120]
        self.assertEqual(current_bar(bars_1430, now=shanghai_at("14:30")).status, BarStatus.FORMING)
        self.assertEqual(current_bar(bars_1430, now=shanghai_at("14:30")).expected_close, shanghai_at("15:00"))

        bars_1500 = resample_minutes(morning_lines() + afternoon_lines(), day=open_day(), now=shanghai_at("15:00"))[Timeframe.MIN_120]
        self.assertEqual(current_bar(bars_1500, now=shanghai_at("15:00")).status, BarStatus.CLOSED)

    def test_missing_delay_suspension_and_short_tail_statuses(self):
        no_data = resample_minutes([], day=open_day(), now=shanghai_at("10:00"))
        self.assertEqual(no_data[Timeframe.MIN_15][0].status, BarStatus.MISSING)
        self.assertEqual(no_data[Timeframe.MIN_15][-1].status, BarStatus.MISSING)

        delayed = resample_minutes(morning_lines("09:50"), day=open_day(), now=shanghai_at("10:00"))
        self.assertEqual(delayed[Timeframe.MIN_15][1].status, BarStatus.MISSING)
        self.assertIsNone(delayed[Timeframe.MIN_15][1].line)

        half = cn_trading_day(date(2026, 9, 14), is_open=True, source="official-calendar", half_day=True)
        half_result = resample_minutes(morning_lines(), day=half, now=shanghai_at("11:30"))
        self.assertEqual(half_result[Timeframe.MIN_120][0].status, BarStatus.CLOSED)
        self.assertEqual(len(half_result[Timeframe.MIN_120]), 1)

        short_session = TradingDay(
            "CN",
            date(2026, 9, 14),
            (SessionWindow("short", shanghai_at("09:30"), shanghai_at("09:45")),),
            "official-calendar",
        )
        short_result = resample_minutes(morning_lines("09:45"), day=short_session, now=shanghai_at("09:45"))
        self.assertEqual(short_result[Timeframe.MIN_30][0].status, BarStatus.INVALID)

        closed = cn_trading_day(date(2026, 9, 14), is_open=False, source="official-calendar")
        self.assertEqual(resample_minutes([], day=closed, now=shanghai_at("10:00"))[Timeframe.MIN_5], ())

    def test_forming_requires_all_due_source_bars_and_closed_requires_all_base_bars(self):
        complete_due = morning_lines("10:30")
        forming = resample_minutes(complete_due, day=open_day(), now=shanghai_at("10:30"))[Timeframe.MIN_120][0]
        self.assertEqual(forming.status, BarStatus.FORMING)
        self.assertIsNone(forming.line)
        self.assertEqual((forming.observed, forming.expected), (12, 24))

        missing_due = morning_lines("10:20") + [line_at("10:30")]
        missing = resample_minutes(missing_due, day=open_day(), now=shanghai_at("10:30"))[Timeframe.MIN_120][0]
        self.assertEqual(missing.status, BarStatus.MISSING)

        incomplete_at_close = resample_minutes(morning_lines("11:25"), day=open_day(), now=shanghai_at("11:30"))[Timeframe.MIN_120][0]
        self.assertEqual(incomplete_at_close.status, BarStatus.MISSING)

    def test_rejects_malformed_inputs_before_any_aggregation(self):
        invalid_cases = [
            "not-a-list",
            [line_at("09:35"), "bad"],
            [line_at("09:35"), line_at("09:35")],
            [line_at("09:40"), line_at("09:35")],
            [line_at("09:31")],
            [KLine("2026-09-14T09:35:01+08:00", 1, 2, 0.5, 1.5, 10)],
            [line_at("11:35")],
            [line_at("12:00")],
            [line_at("15:05")],
            [KLine("2026-09-15T09:35:00+08:00", 1, 2, 0.5, 1.5, 10)],
            [KLine("2026-09-14T09:35:00", 1, 2, 0.5, 1.5, 10)],
            [KLine(shanghai_at("09:35").isoformat(), True, 2, 0.5, 1.5, 10)],
            [KLine(shanghai_at("09:35").isoformat(), 1, math.inf, 0.5, 1.5, 10)],
            [KLine(shanghai_at("09:35").isoformat(), 1, 0.9, 0.5, 1.5, 10)],
            [KLine(shanghai_at("09:35").isoformat(), 1, 2, 0, 1.5, 10)],
            [KLine(shanghai_at("09:35").isoformat(), 1, 2, 0.5, 1.5, -1)],
            [KLine(shanghai_at("09:35").isoformat(), 1, 2, 0.5, 1.5, False)],
        ]
        for lines in invalid_cases:
            with self.subTest(lines=lines):
                with self.assertRaises((TypeError, ValueError)):
                    resample_minutes(lines, day=open_day(), now=shanghai_at("10:00"))

    def test_rejects_future_bars_and_does_not_mutate_input(self):
        lines = morning_lines("09:45")
        original = list(lines)
        with self.assertRaises(ValueError):
            resample_minutes(lines + [line_at("09:50")], day=open_day(), now=shanghai_at("09:45"))

        result = resample_minutes(lines, day=open_day(), now=shanghai_at("09:45"))

        self.assertEqual(lines, original)
        self.assertEqual(result[Timeframe.MIN_15][0].status, BarStatus.CLOSED)

    def test_rejects_nonfinite_aggregate_volume(self):
        lines = [
            KLine(shanghai_at("09:35").isoformat(), 1, 2, 0.5, 1.5, 1e308),
            KLine(shanghai_at("09:40").isoformat(), 1.5, 2.5, 1.0, 2.0, 1e308),
            KLine(shanghai_at("09:45").isoformat(), 2.0, 3.0, 1.5, 2.5, 1e308),
        ]

        with self.assertRaises(ValueError):
            resample_minutes(lines, day=open_day(), now=shanghai_at("09:45"))


if __name__ == "__main__":
    unittest.main()
