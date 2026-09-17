from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import Mock, MagicMock
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.market.global_markets import (
    GlobalMarketDataError,
    ERROR_INSUFFICIENT,
    ERROR_MALFORMED,
    ERROR_SOURCE_ERROR,
    ERROR_NETWORK,
    ERROR_HTTP_STATUS,
    ERROR_REDIRECT,
    ERROR_RATE_LIMITED,
)
from app.market.minute.context import MinuteSnapshot, load_minute_context
from app.market.minute.eastmoney import (
    _secid,
    _normalize_row,
    aggregate_to_5m,
    EastMoneyMinuteProvider,
    create_minute_snapshot_loader,
)
from app.market.minute.session import cn_trading_day
from app.portfolio.analysis import _structure_evidence
from app.portfolio.models import HoldingConfig

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _make_holding(code: str = "600519", market: str = "CN", valuation_mode: str = "exchange") -> HoldingConfig:
    return HoldingConfig(
        code=code,
        name="测试持仓",
        market=market,
        instrument_type="stock",
        valuation_mode=valuation_mode,
        cost_price=Decimal("10"),
        baseline_value=Decimal("1000"),
        sector="technology",
        theme="chips",
    )


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


class EastMoneyAdapterTests(unittest.TestCase):
    def test_secid_mapping(self):
        """1. _secid mapping for Shanghai, Shenzhen and error cases."""
        self.assertEqual(_secid("600519"), "1.600519")
        self.assertEqual(_secid("000001"), "0.000001")
        self.assertEqual(_secid("300750"), "0.300750")
        self.assertEqual(_secid("510300"), "1.510300")
        self.assertEqual(_secid("159915"), "0.159915")
        self.assertEqual(_secid("900901"), "1.900901")
        self.assertEqual(_secid("200002"), "0.200002")

        for bad in ("60051", "6005199", "abc", "60051a", "400001", "800001", "", 123456):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _secid(bad)  # type: ignore

    def test_normalize_row_field_order_and_invariant(self):
        """D1: _normalize_row invariant holds for str and list/tuple in API order."""
        s1 = "2026-09-17 09:31,10.0,10.5,11.0,9.5,100,1000"
        s2 = "2026-09-17 13:05,25.0,24.0,26.5,23.5,500"
        for s in (s1, s2):
            with self.subTest(s=s):
                self.assertEqual(_normalize_row(s), _normalize_row(s.split(",")))
                self.assertEqual(_normalize_row(s), _normalize_row(tuple(s.split(","))))

        # When close and high are swapped such that high < close, raises ERROR_MALFORMED
        # In API order (time, open, close, high, low, volume):
        # Here open=10.0, close=12.0, high=11.0, low=9.0 -> high < close
        swapped_str = "2026-09-17 09:31,10.0,12.0,11.0,9.0,100"
        with self.assertRaises(GlobalMarketDataError) as ctx:
            _normalize_row(swapped_str)
        self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

        with self.assertRaises(GlobalMarketDataError) as ctx:
            _normalize_row(swapped_str.split(","))
        self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

        # Datetime item[0]: naive raises ValueError, aware succeeds
        naive_dt = datetime(2026, 9, 17, 9, 31)
        with self.assertRaises(ValueError):
            _normalize_row([naive_dt, 10.0, 10.5, 11.0, 9.5, 100])

        aware_dt = datetime(2026, 9, 17, 9, 31, tzinfo=SHANGHAI)
        norm_aware = _normalize_row([aware_dt, 10.0, 10.5, 11.0, 9.5, 100])
        self.assertEqual(norm_aware[0], aware_dt)
        self.assertEqual(norm_aware[1:], (10.0, 11.0, 9.5, 10.5, 100.0))

    def test_aggregate_to_5m_ohlcv_and_partial_bucket_drop(self):
        """2. 5m aggregation calculation: correct OHLCV, drop partial buckets, validate malformed."""
        test_day = cn_trading_day(date(2026, 9, 17), is_open=True, source="test")
        now = datetime(2026, 9, 17, 10, 0, tzinfo=SHANGHAI)

        # 5 valid 1-minute bars for 09:31 - 09:35
        # format: "time,open,close,high,low,volume,amount"
        rows_5m = [
            "2026-09-17 09:31,10.0,10.5,11.0,9.5,100,1000",
            "2026-09-17 09:32,10.5,10.8,11.2,10.2,200,2000",
            "2026-09-17 09:33,10.8,10.6,10.9,10.4,150,1500",
            "2026-09-17 09:34,10.6,11.5,11.8,10.5,250,2500",
            "2026-09-17 09:35,11.5,11.2,11.6,11.0,300,3000",
        ]
        res = aggregate_to_5m(rows_5m, day=test_day, now=now)
        self.assertEqual(len(res), 1)
        kline = res[0]
        self.assertEqual(kline.open, 10.0)
        self.assertEqual(kline.high, 11.8)
        self.assertEqual(kline.low, 9.5)
        self.assertEqual(kline.close, 11.2)
        self.assertEqual(kline.volume, 1000.0)
        self.assertEqual(kline.time, "2026-09-17T09:35:00+08:00")

        # Incomplete bucket (only 4 bars, missing 09:35) -> discarded
        rows_incomplete = rows_5m[:4]
        self.assertEqual(aggregate_to_5m(rows_incomplete, day=test_day, now=now), ())

        # Incomplete bucket in second 5m slot: 5 bars for 09:35 + 2 bars for 09:40 -> only first slot returned
        rows_5_plus_2 = rows_5m + [
            "2026-09-17 09:36,11.2,11.3,11.4,11.1,100,1000",
            "2026-09-17 09:37,11.3,11.4,11.5,11.2,100,1000",
        ]
        res_5_plus_2 = aggregate_to_5m(rows_5_plus_2, day=test_day, now=now)
        self.assertEqual(len(res_5_plus_2), 1)

        # Malformed: wrong date
        with self.assertRaises(GlobalMarketDataError) as ctx:
            aggregate_to_5m(["2026-09-16 09:31,10,10,10,10,100"], day=test_day, now=now)
        self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

        # Malformed: future time > now
        future_now = datetime(2026, 9, 17, 9, 32, tzinfo=SHANGHAI)
        with self.assertRaises(GlobalMarketDataError) as ctx:
            aggregate_to_5m(rows_5m, day=test_day, now=future_now)
        self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

        # Malformed: out of session (e.g. 09:29)
        with self.assertRaises(GlobalMarketDataError) as ctx:
            aggregate_to_5m(["2026-09-17 09:29,10,10,10,10,100"], day=test_day, now=now)
        self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

        # Malformed: out of order
        out_of_order = [rows_5m[1], rows_5m[0]]
        with self.assertRaises(GlobalMarketDataError) as ctx:
            aggregate_to_5m(out_of_order, day=test_day, now=now)
        self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

        # Malformed: duplicate timestamp
        dup = [rows_5m[0], rows_5m[0]]
        with self.assertRaises(GlobalMarketDataError) as ctx:
            aggregate_to_5m(dup, day=test_day, now=now)
        self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

        # Malformed: high < max(open, close, low)
        with self.assertRaises(GlobalMarketDataError) as ctx:
            aggregate_to_5m(["2026-09-17 09:31,10,12,11,9,100"], day=test_day, now=now)
        self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

        # Malformed: negative or non-finite price
        with self.assertRaises(GlobalMarketDataError) as ctx:
            aggregate_to_5m(["2026-09-17 09:31,NaN,12,12,9,100"], day=test_day, now=now)
        self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

    def test_http_boundary_and_error_mappings(self):
        """3. Mock session and check HTTP boundaries & exception mappings."""
        provider = EastMoneyMinuteProvider()
        now = datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI)
        holding = _make_holding()

        # Network error -> ERROR_SOURCE_ERROR
        for err_code in (ERROR_NETWORK, ERROR_HTTP_STATUS, ERROR_REDIRECT, ERROR_RATE_LIMITED):
            with self.subTest(err_code=err_code):
                with unittest.mock.patch.object(
                    provider, "_get", side_effect=GlobalMarketDataError(err_code)
                ):
                    with self.assertRaises(GlobalMarketDataError) as ctx:
                        provider.snapshot(holding, now=now)
                    self.assertEqual(ctx.exception.code, ERROR_SOURCE_ERROR)

        # JSON decode error -> ERROR_MALFORMED
        with unittest.mock.patch.object(provider, "_get", return_value=b"not json"):
            with self.assertRaises(GlobalMarketDataError) as ctx:
                provider.snapshot(holding, now=now)
            self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

        # rc != 0 -> ERROR_MALFORMED
        bad_rc = json.dumps({"rc": 100, "data": None}).encode("utf-8")
        with unittest.mock.patch.object(provider, "_get", return_value=bad_rc):
            with self.assertRaises(GlobalMarketDataError) as ctx:
                provider.snapshot(holding, now=now)
            self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

    def test_snapshot_minute_snapshot_contract(self):
        """4. Verify MinuteSnapshot field types, content, and holding validation."""
        provider = EastMoneyMinuteProvider()
        now = datetime(2026, 9, 17, 9, 36, tzinfo=SHANGHAI)
        holding = _make_holding("600519")

        rows = [
            f"2026-09-17 09:3{i},10.0,10.0,10.0,10.0,100,1000"
            for i in range(1, 6)
        ]
        api_data = {"rc": 0, "data": {"klines": rows}}

        def fake_get(url, params=None):
            if "klt=5" in url:
                return json.dumps({"rc": 0, "data": {"klines": []}}).encode("utf-8")
            return json.dumps(api_data).encode("utf-8")

        with unittest.mock.patch.object(provider, "_get", side_effect=fake_get):
            snap = provider.snapshot(holding, now=now)

        self.assertIsInstance(snap, MinuteSnapshot)
        self.assertEqual(snap.code, "600519")
        self.assertEqual(snap.market, "CN")
        self.assertGreater(len(snap.day.sessions), 0)
        self.assertEqual(len(snap.lines), 1)
        self.assertIsInstance(snap.lines, tuple)
        self.assertIsInstance(snap.lines[0], KLine)
        self.assertEqual(snap.source, "eastmoney:push2delay:klt=1->5m")
        self.assertEqual(snap.fetched_at, now)
        self.assertEqual(snap.base_minutes, 5)
        self.assertEqual(snap.timestamp_semantics, "close")

        # Unsupported holdings
        bad_market = _make_holding(market="US")
        with self.assertRaises(ValueError):
            provider.snapshot(bad_market, now=now)

        bad_mode = _make_holding(valuation_mode="otc_fund")
        with self.assertRaises(ValueError):
            provider.snapshot(bad_mode, now=now)

    def test_empty_klines_is_not_evidence_of_closure(self):
        """5. Empty klines is not evidence of market closure; raises ERROR_INSUFFICIENT."""
        provider = EastMoneyMinuteProvider()
        now = datetime(2026, 9, 20, 10, 0, tzinfo=SHANGHAI)  # Sunday
        holding = _make_holding()

        for empty_payload in ({"rc": 0, "data": None}, {"rc": 0, "data": {"klines": []}}):
            with self.subTest(empty_payload=empty_payload):
                with unittest.mock.patch.object(
                    provider, "_get", return_value=json.dumps(empty_payload).encode("utf-8")
                ):
                    with self.assertRaises(GlobalMarketDataError) as ctx:
                        provider.snapshot(holding, now=now)
                    self.assertEqual(ctx.exception.code, ERROR_INSUFFICIENT)

    def test_weekend_stale_klines_raises_malformed(self):
        """Weekend request returning previous trading day klines raises ERROR_MALFORMED."""
        provider = EastMoneyMinuteProvider()
        now = datetime(2026, 9, 19, 10, 0, tzinfo=SHANGHAI)  # Saturday
        holding = _make_holding("600519")

        # Previous trading day: 2026-09-18, 120 klines
        cur_dt = datetime(2026, 9, 18, 9, 31, tzinfo=SHANGHAI)
        stale_rows = []
        for _ in range(120):
            stale_rows.append(f"{cur_dt.strftime('%Y-%m-%d %H:%M')},10.0,10.5,11.0,9.5,100,1000")
            cur_dt += timedelta(minutes=1)

        api_data = {"rc": 0, "data": {"klines": stale_rows}}
        with unittest.mock.patch.object(
            provider, "_get", return_value=json.dumps(api_data).encode("utf-8")
        ):
            with self.assertRaises(GlobalMarketDataError) as ctx:
                provider.snapshot(holding, now=now)
            self.assertEqual(ctx.exception.code, ERROR_MALFORMED)

    def test_end_to_end_context_and_multi_cycle_confirm(self):
        """6. End-to-end integration: 48 5m bars into load_minute_context and _structure_evidence.
        Assert 30m/15m/5m closed, 120m count is 2 (<3), insufficient_cycles contains 120m,
        and multi_cycle_confirm remains False (P2 reachability contract).
        """
        # Construct full trading day 240 1m lines -> 48 5m lines
        # Morning: 09:31 - 11:30 (120 mins = 24 5m bars)
        # Afternoon: 13:01 - 15:00 (120 mins = 24 5m bars)
        rows_1m = []
        cur_dt = datetime(2026, 9, 17, 9, 31, tzinfo=SHANGHAI)
        while cur_dt <= datetime(2026, 9, 17, 11, 30, tzinfo=SHANGHAI):
            rows_1m.append(f"{cur_dt.strftime('%Y-%m-%d %H:%M')},100.0,101.0,102.0,99.0,100,10000")
            cur_dt += timedelta(minutes=1)

        cur_dt = datetime(2026, 9, 17, 13, 1, tzinfo=SHANGHAI)
        while cur_dt <= datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI):
            rows_1m.append(f"{cur_dt.strftime('%Y-%m-%d %H:%M')},101.0,102.0,103.0,100.0,100,10000")
            cur_dt += timedelta(minutes=1)

        self.assertEqual(len(rows_1m), 240)

        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        provider = EastMoneyMinuteProvider()
        api_data = {"rc": 0, "data": {"klines": rows_1m}}

        def fake_get(url, params=None):
            if "klt=5" in url:
                return json.dumps({"rc": 0, "data": {"klines": []}}).encode("utf-8")
            return json.dumps(api_data).encode("utf-8")

        with unittest.mock.patch.object(provider, "_get", side_effect=fake_get):
            snapshot = provider.snapshot(_make_holding("600519"), now=now)

        self.assertEqual(len(snapshot.lines), 48)

        # Context loading
        context = load_minute_context(_make_holding("600519"), snapshot_loader=lambda _h, **_kw: snapshot, now=now)
        self.assertEqual(context["status"], "ready")
        self.assertEqual(context["bar_status"]["5m"], "closed")
        self.assertEqual(context["bar_status"]["15m"], "closed")
        self.assertEqual(context["bar_status"]["30m"], "closed")
        self.assertEqual(context["bar_status"]["120m"], "closed")
        self.assertEqual(len(context["cycles"]["120m"]["closed_lines"]), 2)
        self.assertFalse(context["multi_cycle_confirm"])

        # Bridge to portfolio structure evidence
        evidence = _structure_evidence(
            code="600519",
            market="CN",
            period="daily",
            lines=_daily_lines(),
            cutoff=now,
            trend_confirm=False,
            minute_context=context,
        )
        self.assertIsNotNone(evidence)
        self.assertIn("120m", evidence.insufficient_cycles)
        self.assertFalse(evidence.ready)

    def test_create_minute_snapshot_loader_factory(self):
        """7. create_minute_snapshot_loader returns a callable loader without global state pollution."""
        loader = create_minute_snapshot_loader()
        self.assertTrue(callable(loader))

        # Invoke loader with mock
        now = datetime(2026, 9, 17, 15, 5, tzinfo=SHANGHAI)
        with unittest.mock.patch.object(
            EastMoneyMinuteProvider,
            "snapshot",
            return_value=MinuteSnapshot(
                code="600519",
                market="CN",
                day=cn_trading_day(now.date(), is_open=False, source="mock"),
                lines=(),
                source="mock",
                fetched_at=now,
                base_minutes=5,
                timestamp_semantics="close",
            ),
        ) as mock_snap:
            snap = loader(_make_holding("600519"), now=now)
            mock_snap.assert_called_once()
            self.assertEqual(snap.source, "mock")


if __name__ == "__main__":
    unittest.main()
