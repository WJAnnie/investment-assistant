import unittest

import pandas as pd

from app.market.akshare import AkShareProvider
from app.market.cache import MarketCache
from app.market.collector import MarketCollector, MarketDataUnavailable
from app.market.models import Quote
from app.market.tushare import (
    TushareAPIError,
    TushareConfigurationError,
    TushareProvider,
)
from app.market.validator import validate_quote
from app.chan.models import KLine


class _Provider:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls = 0

    def fetch(self, code):
        self.calls += 1
        if self.error:
            raise self.error
        return self.value


class _KLineProvider:
    def __init__(self, lines=None, error=None):
        self.lines = lines
        self.error = error
        self.calls = 0

    def fetch_klines(self, code, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.lines


class _FakeAkShare:
    def __init__(self):
        self.calls = []

    def stock_zh_a_spot_em(self):
        self.calls.append("a")
        return pd.DataFrame(
            [
                {"代码": "000001", "名称": "平安银行", "最新价": 10.25, "涨跌幅": 1.5},
                {"代码": "000002", "名称": "万科A", "最新价": 8.2, "涨跌幅": -0.4},
            ]
        )

    def fund_etf_spot_em(self):
        self.calls.append("etf")
        return pd.DataFrame(
            [{"代码": "510300", "名称": "沪深300ETF", "最新价": 4.12, "涨跌幅": 0.8}]
        )

    def stock_zh_a_hist(self, **kwargs):
        self.calls.append(("a-history", kwargs))
        return pd.DataFrame(
            [
                {"日期": "2026-09-11", "开盘": 10.2, "收盘": 10.4, "最高": 10.5, "最低": 10.1, "成交量": 1200},
                {"日期": "2026-09-10", "开盘": 10.0, "收盘": 10.2, "最高": 10.3, "最低": 9.9, "成交量": 1100},
            ]
        )

    def fund_etf_hist_em(self, **kwargs):
        self.calls.append(("etf-history", kwargs))
        return pd.DataFrame(
            [{"日期": "2026-09-11", "开盘": 4.2, "收盘": 4.25, "最高": 4.3, "最低": 4.1, "成交量": 1200}]
        )


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def post(self, url, json, timeout):
        self.requests.append((url, json, timeout))
        return _FakeResponse(self.payload)


class _SequenceSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def post(self, url, json, timeout):
        self.requests.append((url, json, timeout))
        return _FakeResponse(self.payloads.pop(0))


class MarketTests(unittest.TestCase):
    def test_cache_supports_deterministic_expiry(self):
        current_time = [100.0]
        cache = MarketCache(clock=lambda: current_time[0])
        cache.set("quote:AAA", "cached")

        self.assertEqual(cache.get("quote:AAA", expire=5), "cached")
        current_time[0] = 106.0
        self.assertIsNone(cache.get("quote:AAA", expire=5))

    def test_validator_rejects_incomplete_or_invalid_quotes(self):
        self.assertTrue(
            validate_quote(
                Quote("AAA", "示例", 10.0, 1.2, "2026-09-11T09:30:00+08:00")
            )
        )
        self.assertFalse(validate_quote({"code": "AAA", "price": -1, "change": 0}))
        self.assertFalse(validate_quote({"price": 10, "change": 1}))

    def test_collector_uses_cache_and_provider_fallback(self):
        primary = _Provider(
            {"code": "AAA", "name": "示例", "price": 10, "change": 1, "timestamp": "now"}
        )
        fallback = _Provider(
            {"code": "AAA", "name": "备用", "price": 11, "change": 2, "timestamp": "now"}
        )
        collector = MarketCollector(primary=primary, fallback=fallback)

        first = collector.get_quote("AAA")
        second = collector.get_quote("AAA")

        self.assertEqual(first, second)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    def test_collector_falls_back_after_primary_failure(self):
        primary = _Provider(error=RuntimeError("primary unavailable"))
        fallback = _Provider(
            {"code": "AAA", "name": "备用", "price": 11, "change": 2, "timestamp": "now"}
        )
        collector = MarketCollector(primary=primary, fallback=fallback)

        quote = collector.get_quote("AAA")

        self.assertEqual(quote.name, "备用")
        self.assertEqual(fallback.calls, 1)

    def test_collector_tries_multiple_fallback_providers_in_order(self):
        primary = _Provider(error=RuntimeError("primary unavailable"))
        first_fallback = _Provider(error=RuntimeError("first fallback unavailable"))
        second_fallback = _Provider(
            {"code": "AAA", "name": "第二备用", "price": 11, "change": 2, "timestamp": "now"}
        )
        collector = MarketCollector(
            primary=primary,
            fallback=[first_fallback, second_fallback],
        )

        quote = collector.get_quote("AAA")

        self.assertEqual(quote.name, "第二备用")
        self.assertEqual(first_fallback.calls, 1)
        self.assertEqual(second_fallback.calls, 1)

    def test_collector_never_fabricates_a_zero_quote(self):
        with self.assertRaises(MarketDataUnavailable) as context:
            MarketCollector().get_quote("AAA")

        self.assertIn("no valid quote available for AAA", str(context.exception))

    def test_collector_reports_provider_errors_when_all_sources_fail(self):
        primary = _Provider(error=RuntimeError("primary unavailable"))
        fallback = _Provider(error=RuntimeError("fallback unavailable"))
        collector = MarketCollector(primary=primary, fallback=fallback)

        with self.assertRaises(MarketDataUnavailable) as context:
            collector.get_quote("AAA")

        message = str(context.exception)
        self.assertIn("primary unavailable", message)
        self.assertIn("fallback unavailable", message)
        self.assertEqual(
            collector.last_errors["quote:AAA"],
            [
                "_Provider: primary unavailable",
                "_Provider: fallback unavailable",
            ],
        )

    def test_collector_gets_valid_klines_from_provider_with_fallback(self):
        primary = _KLineProvider(error=RuntimeError("primary unavailable"))
        fallback_lines = [KLine("20260910", 1, 2, 0.5, 1.5), KLine("20260911", 1.5, 2.5, 1, 2)]
        fallback = _KLineProvider(lines=fallback_lines)
        collector = MarketCollector(primary=primary, fallback=fallback)

        result = collector.get_klines("000001", start_date="20260910", end_date="20260911")

        self.assertEqual(result, fallback_lines)
        self.assertEqual(fallback.calls, 1)


class AkShareProviderTests(unittest.TestCase):
    def test_fetch_normalizes_a_share_snapshot_to_quote_mapping(self):
        fake = _FakeAkShare()
        provider = AkShareProvider(ak_module=fake, clock=lambda: 100.0)

        quote = provider.fetch("000001")

        self.assertEqual(
            quote,
            {
                "code": "000001",
                "name": "平安银行",
                "price": 10.25,
                "change": 1.5,
                "timestamp": "1970-01-01T00:01:40+00:00",
            },
        )
        self.assertEqual(fake.calls, ["a"])

    def test_fetch_dispatches_etf_and_rejects_hk_codes(self):
        fake = _FakeAkShare()
        provider = AkShareProvider(ak_module=fake, clock=lambda: 100.0)

        etf = provider.fetch("SH.510300")
        with self.assertRaises(ValueError):
            provider.fetch("00700.HK")

        self.assertEqual(etf["name"], "沪深300ETF")
        self.assertEqual(etf["code"], "SH.510300")
        self.assertEqual(fake.calls, ["etf"])

    def test_fetch_reuses_snapshot_for_multiple_codes_within_ttl(self):
        fake = _FakeAkShare()
        current_time = [100.0]
        provider = AkShareProvider(
            ak_module=fake,
            clock=lambda: current_time[0],
            snapshot_ttl=30,
        )

        provider.fetch("000001")
        provider.fetch("000002")
        current_time[0] = 131.0
        provider.fetch("000001")

        self.assertEqual(fake.calls, ["a", "a"])

    def test_fetch_returns_none_for_blank_or_unknown_codes(self):
        provider = AkShareProvider(ak_module=_FakeAkShare())

        self.assertIsNone(provider.fetch(""))
        self.assertIsNone(provider.fetch("999999"))

    def test_fetch_klines_normalizes_history_and_sorts_oldest_first(self):
        fake = _FakeAkShare()
        provider = AkShareProvider(ak_module=fake)

        lines = provider.fetch_klines(
            "000001",
            start_date="20260910",
            end_date="20260911",
            adjust="qfq",
        )

        self.assertEqual(lines, [
            KLine("2026-09-10", 10.0, 10.3, 9.9, 10.2, 1100),
            KLine("2026-09-11", 10.2, 10.5, 10.1, 10.4, 1200),
        ])
        self.assertEqual(fake.calls[0], ("a-history", {
            "symbol": "000001",
            "period": "daily",
            "start_date": "20260910",
            "end_date": "20260911",
            "adjust": "qfq",
        }))

    def test_fetch_klines_dispatches_etf_and_rejects_hk_history(self):
        fake = _FakeAkShare()
        provider = AkShareProvider(ak_module=fake)

        etf = provider.fetch_klines("510300", start_date="20260911", end_date="20260911")
        with self.assertRaises(ValueError):
            provider.fetch_klines("00700.HK", start_date="20260911", end_date="20260911")

        self.assertEqual(etf[0].close, 4.25)
        self.assertEqual(fake.calls[0][0], "etf-history")


class TushareProviderTests(unittest.TestCase):
    def test_fetch_returns_latest_close_as_a_non_realtime_fallback_quote(self):
        session = _FakeSession(
            {
                "code": 0,
                "data": {
                    "fields": ["ts_code", "trade_date", "close", "pct_chg"],
                    "items": [
                        ["000001.SZ", "20260911", 10.4, 1.7],
                        ["000001.SZ", "20260910", 10.2, 0.8],
                    ],
                },
            }
        )
        provider = TushareProvider(token="secret-token", session=session)

        quote = provider.fetch("000001")

        self.assertEqual(quote, Quote("000001", "000001", 10.4, 1.7, "20260911"))
        self.assertEqual(session.requests[-1][1]["api_name"], "daily")

    def test_fetch_prefers_latest_realtime_minute_quote(self):
        realtime_payload = {
            "code": 0,
            "data": {
                "fields": ["ts_code", "time", "open", "high", "low", "close", "vol"],
                "items": [
                    ["000001.SZ", "2026-09-11 09:30:00", 10.1, 10.2, 10.0, 10.2, 1100],
                    ["000001.SZ", "2026-09-11 09:31:00", 10.2, 10.3, 10.1, 10.25, 1200],
                ],
            },
        }
        session = _SequenceSession([realtime_payload])
        provider = TushareProvider(token="secret-token", session=session)

        quote = provider.fetch("000001")

        self.assertEqual(
            quote,
            Quote("000001", "000001", 10.25, 0.0, "2026-09-11 09:31:00"),
        )
        self.assertEqual(session.requests[0][1]["api_name"], "rt_min_daily")

    def test_query_translates_tushare_rows_and_sends_token_payload(self):
        session = _FakeSession(
            {
                "code": 0,
                "msg": None,
                "data": {
                    "fields": ["ts_code", "trade_date", "close"],
                    "items": [["000001.SZ", "20260910", 10.25]],
                },
            }
        )
        provider = TushareProvider(token="secret-token", session=session)

        rows = provider.query(
            "daily",
            params={"ts_code": "000001.SZ"},
            fields="ts_code,trade_date,close",
        )

        self.assertEqual(rows, [{"ts_code": "000001.SZ", "trade_date": "20260910", "close": 10.25}])
        self.assertEqual(
            session.requests,
            [
                (
                    "https://api.tushare.pro",
                    {
                        "api_name": "daily",
                        "token": "secret-token",
                        "params": {"ts_code": "000001.SZ"},
                        "fields": "ts_code,trade_date,close",
                    },
                    10,
                )
            ],
        )

    def test_fetch_klines_maps_codes_and_sorts_rows_for_chan_analysis(self):
        session = _FakeSession(
            {
                "code": 0,
                "data": {
                    "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol"],
                    "items": [
                        ["510300.SH", "20260911", 4.2, 4.3, 4.1, 4.25, 1200],
                        ["510300.SH", "20260910", 4.1, 4.25, 4.0, 4.2, 1100],
                    ],
                },
            }
        )
        provider = TushareProvider(token="secret-token", session=session)

        lines = provider.fetch_klines("510300", start_date="20260910", end_date="20260911")

        self.assertEqual(lines, [
            KLine("20260910", 4.1, 4.25, 4.0, 4.2, 1100),
            KLine("20260911", 4.2, 4.3, 4.1, 4.25, 1200),
        ])
        self.assertEqual(session.requests[0][1]["api_name"], "fund_daily")
        self.assertEqual(session.requests[0][1]["params"]["ts_code"], "510300.SH")

    def test_fetch_minute_klines_uses_realtime_tushare_endpoint(self):
        session = _FakeSession(
            {
                "code": 0,
                "data": {
                    "fields": ["ts_code", "time", "open", "high", "low", "close", "vol"],
                    "items": [
                        ["000001.SZ", "2026-09-11 09:31:00", 10.2, 10.3, 10.1, 10.25, 1200],
                        ["000001.SZ", "2026-09-11 09:30:00", 10.1, 10.2, 10.0, 10.2, 1100],
                    ],
                },
            }
        )
        provider = TushareProvider(token="secret-token", session=session)

        lines = provider.fetch_klines("000001", period="5")

        self.assertEqual(lines, [
            KLine("2026-09-11 09:30:00", 10.1, 10.2, 10.0, 10.2, 1100),
            KLine("2026-09-11 09:31:00", 10.2, 10.3, 10.1, 10.25, 1200),
        ])
        self.assertEqual(session.requests[0][1]["api_name"], "rt_min_daily")
        self.assertEqual(
            session.requests[0][1]["params"],
            {"ts_code": "000001.SZ", "freq": "5MIN"},
        )

    def test_fetch_rejects_unsupported_realtime_period(self):
        provider = TushareProvider(token="secret-token", session=_FakeSession({}))

        with self.assertRaises(ValueError):
            provider.fetch_klines("000001", period="2")

    def test_query_rejects_missing_token_and_api_errors(self):
        with self.assertRaises(TushareConfigurationError):
            TushareProvider(session=_FakeSession({})).query("daily")

        session = _FakeSession({"code": 2002, "msg": "没有权限"})
        with self.assertRaises(TushareAPIError):
            TushareProvider(token="secret-token", session=session).query("daily")


if __name__ == "__main__":
    unittest.main()
