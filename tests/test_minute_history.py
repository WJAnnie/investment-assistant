"""Unit tests for multi-day 5m historical row parsing, 120m aggregation, and context assembly.

Strict offline tests using fixtures only (no real network requests).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import unittest
import unittest.mock
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.domain.bars import BarStatus
from app.domain.timeframe import Timeframe
from app.market.global_markets import (
    ERROR_MALFORMED,
    ERROR_SOURCE_ERROR,
    GlobalMarketDataError,
)
from app.market.minute.context import MinuteSnapshot, load_minute_context
from app.market.minute.eastmoney import EastMoneyMinuteProvider
from app.market.minute.history import (
    SHANGHAI,
    build_history_120m,
    parse_history_rows,
)
from app.market.minute.session import cn_trading_day
from app.portfolio.analysis import _structure_evidence


def _make_holding(code: str = "600519", market: str = "CN", valuation_mode: str = "exchange"):
    class Holding:
        pass

    h = Holding()
    h.code = code
    h.market = market
    h.valuation_mode = valuation_mode
    return h


def _generate_5m_rows(day_str: str, base_price: float = 100.0) -> list[str]:
    """Generate 48 standard 5m rows for a trading day (API format: time,open,close,high,low,volume)."""
    rows = []
    # Morning: 09:35 to 11:30 (24 bars)
    cur = datetime.strptime(f"{day_str} 09:35", "%Y-%m-%d %H:%M")
    end_morning = datetime.strptime(f"{day_str} 11:30", "%Y-%m-%d %H:%M")
    p = base_price
    while cur <= end_morning:
        rows.append(f"{cur.strftime('%Y-%m-%d %H:%M')},{p:.2f},{p+0.5:.2f},{p+1.0:.2f},{p-0.5:.2f},100,10000")
        p += 0.2
        cur += timedelta(minutes=5)

    # Afternoon: 13:05 to 15:00 (24 bars)
    cur = datetime.strptime(f"{day_str} 13:05", "%Y-%m-%d %H:%M")
    end_afternoon = datetime.strptime(f"{day_str} 15:00", "%Y-%m-%d %H:%M")
    while cur <= end_afternoon:
        rows.append(f"{cur.strftime('%Y-%m-%d %H:%M')},{p:.2f},{p+0.5:.2f},{p+1.0:.2f},{p-0.5:.2f},100,10000")
        p += 0.2
        cur += timedelta(minutes=5)

    return rows


def _generate_5m_klines(day_str: str, base_price: float = 100.0) -> list[KLine]:
    rows = _generate_5m_rows(day_str, base_price=base_price)
    return list(parse_history_rows(rows))


def _daily_lines(count: int = 35) -> tuple[KLine, ...]:
    lines = []
    base_dt = datetime(2026, 8, 1, 15, 0, tzinfo=SHANGHAI)
    for i in range(count):
        dt = base_dt + timedelta(days=i)
        lines.append(
            KLine(
                time=dt.isoformat(),
                open=100.0 + i,
                high=105.0 + i,
                low=95.0 + i,
                close=102.0 + i,
                volume=10000.0 + i * 100,
            )
        )
    return tuple(lines)


# =========================================================================
# 1. parse_history_rows tests
# =========================================================================
class ParseHistoryRowsTests(unittest.TestCase):
    def test_parse_history_rows_normal_multi_day(self):
        day1 = _generate_5m_rows("2026-09-15", 100.0)
        day2 = _generate_5m_rows("2026-09-16", 110.0)
        all_rows = day1 + day2
        klines = parse_history_rows(all_rows)
        self.assertEqual(len(klines), 96)
        self.assertIsInstance(klines, tuple)
        self.assertIsInstance(klines[0], KLine)
        # Check first and last time
        self.assertEqual(klines[0].time, "2026-09-15T09:35:00+08:00")
        self.assertEqual(klines[-1].time, "2026-09-16T15:00:00+08:00")

    def test_parse_history_rows_column_order(self):
        # API order: time, open, close, high, low, volume
        row = ["2026-09-16 09:35,10.0,12.0,15.0,8.0,250"]
        klines = parse_history_rows(row)
        self.assertEqual(len(klines), 1)
        k = klines[0]
        self.assertEqual(k.open, 10.0)
        self.assertEqual(k.close, 12.0)
        self.assertEqual(k.high, 15.0)
        self.assertEqual(k.low, 8.0)
        self.assertEqual(k.volume, 250.0)

    def test_parse_history_rows_rejects_unaligned_5m(self):
        # 09:31 is not aligned to 5m close
        bad_row = ["2026-09-16 09:31,10.0,10.0,10.0,10.0,100"]
        with self.assertRaises(ValueError) as ctx:
            parse_history_rows(bad_row)
        self.assertIn("must align", str(ctx.exception))

    def test_parse_history_rows_trading_hours_boundaries(self):
        # 11:30 is legal
        k_1130 = parse_history_rows(["2026-09-16 11:30,10.0,10.0,10.0,10.0,100"])
        self.assertEqual(len(k_1130), 1)
        # 13:00 is legal
        k_1300 = parse_history_rows(["2026-09-16 13:00,10.0,10.0,10.0,10.0,100"])
        self.assertEqual(len(k_1300), 1)
        # 15:00 is legal
        k_1500 = parse_history_rows(["2026-09-16 15:00,10.0,10.0,10.0,10.0,100"])
        self.assertEqual(len(k_1500), 1)
        # 11:35 is rejected
        with self.assertRaises(ValueError):
            parse_history_rows(["2026-09-16 11:35,10.0,10.0,10.0,10.0,100"])
        # 09:30 is rejected (first 5m close is 09:35)
        with self.assertRaises(ValueError):
            parse_history_rows(["2026-09-16 09:30,10.0,10.0,10.0,10.0,100"])
        # 15:05 is rejected
        with self.assertRaises(ValueError):
            parse_history_rows(["2026-09-16 15:05,10.0,10.0,10.0,10.0,100"])

    def test_parse_history_rows_rejects_non_increasing(self):
        rows = [
            "2026-09-16 09:40,10.0,10.0,10.0,10.0,100",
            "2026-09-16 09:35,10.0,10.0,10.0,10.0,100",
        ]
        with self.assertRaises(ValueError) as ctx:
            parse_history_rows(rows)
        self.assertIn("strictly increasing", str(ctx.exception))

    def test_parse_history_rows_rejects_duplicates(self):
        rows = [
            "2026-09-16 09:35,10.0,10.0,10.0,10.0,100",
            "2026-09-16 09:35,10.0,10.0,10.0,10.0,100",
        ]
        with self.assertRaises(ValueError) as ctx:
            parse_history_rows(rows)
        self.assertIn("strictly increasing", str(ctx.exception))

    def test_parse_history_rows_rejects_ohlc_violations(self):
        cases = [
            ("high < open", "2026-09-16 09:35,10.0,9.0,8.0,7.0,100"),
            ("high < close", "2026-09-16 09:35,8.0,12.0,10.0,7.0,100"),
            ("low > open", "2026-09-16 09:35,8.0,12.0,15.0,9.0,100"),
            ("low > close", "2026-09-16 09:35,12.0,8.0,15.0,9.0,100"),
            ("zero price", "2026-09-16 09:35,0.0,10.0,10.0,0.0,100"),
            ("negative price", "2026-09-16 09:35,-10.0,10.0,10.0,-10.0,100"),
            ("nan price", "2026-09-16 09:35,nan,10.0,10.0,10.0,100"),
            ("inf price", "2026-09-16 09:35,inf,10.0,inf,10.0,100"),
        ]
        for name, row in cases:
            with self.subTest(case=name):
                with self.assertRaises((ValueError, TypeError)):
                    parse_history_rows([row])

    def test_parse_history_rows_rejects_negative_volume(self):
        bad_vol = ["2026-09-16 09:35,10.0,10.0,10.0,10.0,-1"]
        with self.assertRaises(ValueError) as ctx:
            parse_history_rows(bad_vol)
        self.assertIn("non-negative", str(ctx.exception))

    def test_parse_history_rows_rejects_non_string_or_insufficient_columns(self):
        with self.assertRaises(TypeError):
            parse_history_rows([12345])  # not string
        with self.assertRaises(TypeError):
            parse_history_rows("2026-09-16 09:35,10,10,10,10,100")  # bare string not list
        with self.assertRaises(TypeError):
            parse_history_rows(None)
        with self.assertRaises(ValueError):
            parse_history_rows(["2026-09-16 09:35,10.0,10.0,10.0,10.0"])  # 5 columns only

    def test_parse_history_rows_rejects_future_time(self):
        now = datetime(2026, 9, 16, 10, 0, tzinfo=SHANGHAI)
        future_row = ["2026-09-16 10:05,10.0,10.0,10.0,10.0,100"]
        with self.assertRaises(ValueError) as ctx:
            parse_history_rows(future_row, now=now)
        self.assertIn("exceeds now", str(ctx.exception))


# =========================================================================
# 2. build_history_120m tests
# =========================================================================
class BuildHistory120mTests(unittest.TestCase):
    def test_build_history_120m_full_two_days(self):
        lines1 = _generate_5m_klines("2026-09-15", 100.0)
        lines2 = _generate_5m_klines("2026-09-16", 110.0)
        now = datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI)
        res = build_history_120m(lines1 + lines2, now=now)
        self.assertEqual(len(res), 4)
        self.assertEqual(res[0].time, "2026-09-15T11:30:00+08:00")
        self.assertEqual(res[1].time, "2026-09-15T15:00:00+08:00")
        self.assertEqual(res[2].time, "2026-09-16T11:30:00+08:00")
        self.assertEqual(res[3].time, "2026-09-16T15:00:00+08:00")

    def test_build_history_120m_single_session(self):
        # 1 day morning session only (24 bars)
        lines = _generate_5m_klines("2026-09-15", 100.0)[:24]
        now = datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI)
        res = build_history_120m(lines, now=now)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].time, "2026-09-15T11:30:00+08:00")

    def test_build_history_120m_incomplete_bucket_discarded(self):
        # Morning has 23 bars (missing 1), afternoon has 24 bars
        all_lines = _generate_5m_klines("2026-09-15", 100.0)
        incomplete_morning = all_lines[1:24]  # 23 bars
        afternoon = all_lines[24:]            # 24 bars
        now = datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI)
        res = build_history_120m(incomplete_morning + afternoon, now=now)
        # Morning bucket discarded! Only afternoon survives
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].time, "2026-09-15T15:00:00+08:00")

    def test_build_history_120m_strictly_increasing_across_days(self):
        lines1 = _generate_5m_klines("2026-09-14", 100.0)
        lines2 = _generate_5m_klines("2026-09-15", 105.0)
        lines3 = _generate_5m_klines("2026-09-16", 110.0)
        now = datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI)
        res = build_history_120m(lines1 + lines2 + lines3, now=now)
        self.assertEqual(len(res), 6)
        for i in range(len(res) - 1):
            t_curr = datetime.fromisoformat(res[i].time)
            t_next = datetime.fromisoformat(res[i + 1].time)
            self.assertLess(t_curr, t_next)

    def test_build_history_120m_excludes_current_day(self):
        lines_yesterday = _generate_5m_klines("2026-09-16", 100.0)
        lines_today = _generate_5m_klines("2026-09-17", 110.0)
        now = datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI)
        res = build_history_120m(lines_yesterday + lines_today, now=now)
        # Today's bars are excluded from historical 120m synthesis
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0].time, "2026-09-16T11:30:00+08:00")
        self.assertEqual(res[1].time, "2026-09-16T15:00:00+08:00")

    def test_build_history_120m_aggregation_values(self):
        # Manually craft 24 morning bars with distinct OHLCV
        bars = []
        cur = datetime(2026, 9, 16, 9, 35, tzinfo=SHANGHAI)
        for i in range(24):
            bars.append(
                KLine(
                    time=cur.isoformat(),
                    open=10.0 if i == 0 else 15.0,
                    high=30.0 if i == 5 else (26.0 if i == 23 else 20.0),
                    low=5.0 if i == 10 else (8.0 if i == 0 else 12.0),
                    close=25.0 if i == 23 else 18.0,
                    volume=100.0,
                )
            )
            cur += timedelta(minutes=5)
        now = datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI)
        res = build_history_120m(bars, now=now)
        self.assertEqual(len(res), 1)
        b = res[0]
        self.assertEqual(b.open, 10.0)   # first bar open
        self.assertEqual(b.high, 30.0)   # max high
        self.assertEqual(b.low, 5.0)     # min low
        self.assertEqual(b.close, 25.0)  # last bar close
        self.assertEqual(b.volume, 2400.0)  # sum of 24 * 100

    def test_build_history_120m_empty_input(self):
        now = datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI)
        self.assertEqual(build_history_120m([], now=now), ())
        self.assertEqual(build_history_120m((), now=now), ())

    def test_build_history_120m_future_line_rejected(self):
        lines = _generate_5m_klines("2026-09-17", 100.0)
        # now is 10:00, but lines has afternoon 15:00
        now = datetime(2026, 9, 17, 10, 0, tzinfo=SHANGHAI)
        with self.assertRaises(ValueError) as ctx:
            build_history_120m(lines, now=now)
        self.assertIn("exceeds current time", str(ctx.exception))


# =========================================================================
# 3. MinuteSnapshot history_120m_lines validation tests
# =========================================================================
class MinuteSnapshotHistoryValidationTests(unittest.TestCase):
    def test_snapshot_default_history_empty_tuple(self):
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        snap = MinuteSnapshot(
            code="600519",
            market="CN",
            day=cn_trading_day(now.date(), is_open=True, source="test"),
            lines=(),
            source="test",
            fetched_at=now,
            base_minutes=5,
            timestamp_semantics="close",
        )
        self.assertEqual(snap.history_120m_lines, ())

    def test_snapshot_history_same_day_rejected(self):
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        holding = _make_holding("600519")
        # History bar on same date 2026-09-17
        bad_bar = KLine(
            time="2026-09-17T11:30:00+08:00",
            open=10.0, high=12.0, low=9.0, close=11.0, volume=100.0,
        )
        snap = MinuteSnapshot(
            code="600519",
            market="CN",
            day=cn_trading_day(now.date(), is_open=True, source="test"),
            lines=(),
            source="test",
            fetched_at=now,
            base_minutes=5,
            timestamp_semantics="close",
            history_120m_lines=(bad_bar,),
        )
        ctx = load_minute_context(holding, snapshot_loader=lambda _h, **_kw: snap, now=now)
        self.assertEqual(ctx["status"], "unavailable")

    def test_snapshot_history_non_increasing_rejected(self):
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        holding = _make_holding("600519")
        b1 = KLine(time="2026-09-16T15:00:00+08:00", open=10, high=12, low=9, close=11, volume=100)
        b2 = KLine(time="2026-09-16T11:30:00+08:00", open=10, high=12, low=9, close=11, volume=100)
        snap = MinuteSnapshot(
            code="600519",
            market="CN",
            day=cn_trading_day(now.date(), is_open=True, source="test"),
            lines=(),
            source="test",
            fetched_at=now,
            base_minutes=5,
            timestamp_semantics="close",
            history_120m_lines=(b1, b2),  # non-increasing!
        )
        ctx = load_minute_context(holding, snapshot_loader=lambda _h, **_kw: snap, now=now)
        self.assertEqual(ctx["status"], "unavailable")

    def test_snapshot_history_newer_than_fetched_at_rejected(self):
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        fetched_at = datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI)
        holding = _make_holding("600519")
        # Future history bar
        b_future = KLine(time="2026-09-18T11:30:00+08:00", open=10, high=12, low=9, close=11, volume=100)
        snap = MinuteSnapshot(
            code="600519",
            market="CN",
            day=cn_trading_day(now.date(), is_open=True, source="test"),
            lines=(),
            source="test",
            fetched_at=fetched_at,
            base_minutes=5,
            timestamp_semantics="close",
            history_120m_lines=(b_future,),
        )
        ctx = load_minute_context(holding, snapshot_loader=lambda _h, **_kw: snap, now=now)
        self.assertEqual(ctx["status"], "unavailable")

    def test_snapshot_history_invalid_bucket_time_rejected(self):
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        holding = _make_holding("600519")
        # 10:30 is not a valid 120m bucket close (must be 11:30 or 15:00)
        bad_bucket = KLine(time="2026-09-16T10:30:00+08:00", open=10, high=12, low=9, close=11, volume=100)
        snap = MinuteSnapshot(
            code="600519",
            market="CN",
            day=cn_trading_day(now.date(), is_open=True, source="test"),
            lines=(),
            source="test",
            fetched_at=now,
            base_minutes=5,
            timestamp_semantics="close",
            history_120m_lines=(bad_bucket,),
        )
        ctx = load_minute_context(holding, snapshot_loader=lambda _h, **_kw: snap, now=now)
        self.assertEqual(ctx["status"], "unavailable")

    def test_snapshot_history_non_kline_rejected(self):
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        holding = _make_holding("600519")
        snap = MinuteSnapshot(
            code="600519",
            market="CN",
            day=cn_trading_day(now.date(), is_open=True, source="test"),
            lines=(),
            source="test",
            fetched_at=now,
            base_minutes=5,
            timestamp_semantics="close",
            history_120m_lines=("not a kline",),
        )
        ctx = load_minute_context(holding, snapshot_loader=lambda _h, **_kw: snap, now=now)
        self.assertEqual(ctx["status"], "unavailable")


# =========================================================================
# 4. EastMoney fetch_history_rows & snapshot tests
# =========================================================================
class EastMoneyFetchHistoryRowsTests(unittest.TestCase):
    def test_fetch_history_rows_url_and_params(self):
        provider = EastMoneyMinuteProvider()
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        captured_url = []

        def mock_get(url, params=None):
            captured_url.append(url)
            return json.dumps({"rc": 0, "data": {"klines": []}}).encode("utf-8")

        with unittest.mock.patch.object(provider, "_get", side_effect=mock_get):
            res = provider.fetch_history_rows(
                "600519",
                now=now,
                beg="20260913",
                end="20260917",
            )
        self.assertEqual(res, [])
        self.assertEqual(len(captured_url), 1)
        u = captured_url[0]
        self.assertIn("push2his.eastmoney.com", u)
        self.assertIn("secid=1.600519", u)
        self.assertIn("klt=5", u)
        self.assertIn("beg=20260913", u)
        self.assertIn("end=20260917", u)

    def test_fetch_history_rows_network_error_mapped(self):
        provider = EastMoneyMinuteProvider()
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        with unittest.mock.patch.object(
            provider, "_get", side_effect=GlobalMarketDataError(ERROR_SOURCE_ERROR)
        ):
            with self.assertRaises(GlobalMarketDataError) as ctx:
                provider.fetch_history_rows("600519", now=now, beg="20260913", end="20260917")
            self.assertEqual(ctx.exception.code, ERROR_SOURCE_ERROR)

    def test_fetch_history_rows_fixture_parsing(self):
        provider = EastMoneyMinuteProvider()
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        fixture_rows = [
            "2026-09-16 09:35,100.0,101.0,102.0,99.0,100,1000",
            "2026-09-16 09:40,101.0,102.0,103.0,100.0,100,1000",
        ]
        payload = json.dumps({"rc": 0, "data": {"klines": fixture_rows}}).encode("utf-8")
        with unittest.mock.patch.object(provider, "_get", return_value=payload):
            rows = provider.fetch_history_rows("600519", now=now, beg="20260913", end="20260917")
        self.assertEqual(rows, fixture_rows)

    def test_snapshot_history_fetch_failure_fail_closed(self):
        provider = EastMoneyMinuteProvider()
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        holding = _make_holding("600519")

        # push2delay succeeds, but push2his fails
        rows_1m = [
            f"2026-09-17 09:3{i},10.0,10.0,10.0,10.0,100,1000"
            for i in range(1, 6)
        ]
        delay_payload = json.dumps({"rc": 0, "data": {"klines": rows_1m}}).encode("utf-8")

        def side_effect(url, params=None):
            if "push2his" in url:
                raise GlobalMarketDataError(ERROR_SOURCE_ERROR)
            return delay_payload

        with unittest.mock.patch.object(provider, "_get", side_effect=side_effect):
            # Must fail closed: snapshot raises GlobalMarketDataError
            with self.assertRaises(GlobalMarketDataError):
                provider.snapshot(holding, now=now)

    def test_snapshot_weekend_empty_history_allowed(self):
        provider = EastMoneyMinuteProvider()
        now = datetime(2026, 9, 17, 9, 36, tzinfo=SHANGHAI)
        holding = _make_holding("600519")

        rows_1m = [
            f"2026-09-17 09:3{i},10.0,10.0,10.0,10.0,100,1000"
            for i in range(1, 6)
        ]
        delay_payload = json.dumps({"rc": 0, "data": {"klines": rows_1m}}).encode("utf-8")
        his_payload = json.dumps({"rc": 0, "data": {"klines": []}}).encode("utf-8")

        def side_effect(url, params=None):
            if "push2his" in url:
                return his_payload
            return delay_payload

        with unittest.mock.patch.object(provider, "_get", side_effect=side_effect):
            snap = provider.snapshot(holding, now=now)
        self.assertEqual(snap.history_120m_lines, ())


# =========================================================================
# 5. Context assembly & integration tests
# =========================================================================
class ContextAssemblyAndIntegrationTests(unittest.TestCase):
    def test_context_assembly_with_history_120m_reaches_contract(self):
        # 48 5m lines today (closed at 15:00) -> 2 closed 120m lines today
        day_str = "2026-09-17"
        lines_today = _generate_5m_klines(day_str, 100.0)
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)

        # 2 historical 120m lines from yesterday (2026-09-16)
        hist_b1 = KLine(time="2026-09-16T11:30:00+08:00", open=95, high=98, low=94, close=97, volume=2400)
        hist_b2 = KLine(time="2026-09-16T15:00:00+08:00", open=97, high=100, low=96, close=99, volume=2400)

        snap = MinuteSnapshot(
            code="600519",
            market="CN",
            day=cn_trading_day(now.date(), is_open=True, source="test"),
            lines=lines_today,
            source="eastmoney:push2delay:klt=1->5m",
            fetched_at=now,
            base_minutes=5,
            timestamp_semantics="close",
            history_120m_lines=(hist_b1, hist_b2),
        )

        ctx = load_minute_context(_make_holding("600519"), snapshot_loader=lambda _h, **_kw: snap, now=now)
        self.assertEqual(ctx["status"], "ready")
        closed_120m = ctx["cycles"]["120m"]["closed_lines"]
        # 2 history + 2 today = 4 closed 120m bars! (>= 3)
        self.assertEqual(len(closed_120m), 4)

        # Strictly increasing check
        times = [datetime.fromisoformat(b.time) for b in closed_120m]
        for i in range(len(times) - 1):
            self.assertLess(times[i], times[i + 1])

        # Other cycles unchanged
        self.assertEqual(len(ctx["cycles"]["5m"]["closed_lines"]), 48)
        self.assertEqual(len(ctx["cycles"]["15m"]["closed_lines"]), 16)
        self.assertEqual(len(ctx["cycles"]["30m"]["closed_lines"]), 8)

    def test_context_assembly_without_history_keeps_two_bars(self):
        day_str = "2026-09-17"
        lines_today = _generate_5m_klines(day_str, 100.0)
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)

        snap = MinuteSnapshot(
            code="600519",
            market="CN",
            day=cn_trading_day(now.date(), is_open=True, source="test"),
            lines=lines_today,
            source="eastmoney:push2delay:klt=1->5m",
            fetched_at=now,
            base_minutes=5,
            timestamp_semantics="close",
            history_120m_lines=(),
        )

        ctx = load_minute_context(_make_holding("600519"), snapshot_loader=lambda _h, **_kw: snap, now=now)
        self.assertEqual(ctx["status"], "ready")
        self.assertEqual(len(ctx["cycles"]["120m"]["closed_lines"]), 2)

    def test_end_to_end_minute_snapshot_to_context_and_structure_evidence(self):
        # 48 5m lines today + 48 5m lines yesterday synthesized into 2 120m history bars
        lines_yesterday = _generate_5m_klines("2026-09-16", 95.0)
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        history_120m = build_history_120m(lines_yesterday, now=now)
        self.assertEqual(len(history_120m), 2)

        lines_today = _generate_5m_klines("2026-09-17", 100.0)
        snap = MinuteSnapshot(
            code="600519",
            market="CN",
            day=cn_trading_day(now.date(), is_open=True, source="test"),
            lines=lines_today,
            source="eastmoney:push2delay:klt=1->5m",
            fetched_at=now,
            base_minutes=5,
            timestamp_semantics="close",
            history_120m_lines=history_120m,
        )

        ctx = load_minute_context(_make_holding("600519"), snapshot_loader=lambda _h, **_kw: snap, now=now)
        self.assertEqual(ctx["status"], "ready")
        self.assertEqual(len(ctx["cycles"]["120m"]["closed_lines"]), 4)

        # Bridge to portfolio structure evidence
        evidence = _structure_evidence(
            code="600519",
            market="CN",
            period="daily",
            lines=_daily_lines(),
            cutoff=now,
            trend_confirm=False,
            minute_context=ctx,
        )
        self.assertIsNotNone(evidence)
        # 120m is NO LONGER insufficient because count is 4 (>= min_bars 3)!
        self.assertNotIn("120m", evidence.insufficient_cycles)


if __name__ == "__main__":
    unittest.main()
