from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import sys
from types import MappingProxyType
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from app.analysis.fundamental import (
    StockFundamentalObservation,
    StockValuationObservation,
)
from app.market.fundamentals import (
    ERROR_MALFORMED_RESPONSE,
    ERROR_NETWORK_UNAVAILABLE,
    ERROR_NO_USABLE_FUNDAMENTAL,
    ERROR_NO_USABLE_VALUATION,
    ERROR_SYMBOL_MISMATCH,
    AkShareStockEvidenceProvider,
    StockEvidenceFetch,
    _parse_date,
    _parse_numeric,
    _parse_shanghai_datetime,
    _parse_str,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _shanghai_dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=SHANGHAI_TZ)


def _utc_dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _valid_fundamental_df(
    symbol: str = "600000",
    mapped_symbol: str = "600000.SH",
    report_date: str = "2024-06-30",
    notice_date: str = "2024-08-25",
    revenue_growth: float = 12.5,
    profit_growth: float = 8.3,
    roe: float = 10.2,
    eps: float = 0.85,
) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "SECUCODE": mapped_symbol,
            "SECURITY_CODE": symbol,
            "REPORT_DATE": report_date,
            "NOTICE_DATE": notice_date,
            "TOTALOPERATEREVETZ": revenue_growth,
            "PARENTNETPROFITTZ": profit_growth,
            "ROEJQ": roe,
            "EPSJB": eps,
        }
    ])


def _valid_valuation_df(
    date_str: str = "2024-08-25",
    pe_ttm: float = 5.6,
    pb: float = 0.55,
    ps: float = 1.2,
) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "数据日期": date_str,
            "PE(TTM)": pe_ttm,
            "市净率": pb,
            "市销率": ps,
        }
    ])


class FakeAkShareClient:
    def __init__(self, fundamental_data=None, valuation_data=None) -> None:
        self.fundamental_data = fundamental_data
        self.valuation_data = valuation_data
        self.calls: list[tuple[str, dict]] = []

    def stock_financial_analysis_indicator_em(self, symbol: str, indicator: str = "按报告期") -> pd.DataFrame:
        self.calls.append(("stock_financial_analysis_indicator_em", {"symbol": symbol, "indicator": indicator}))
        if isinstance(self.fundamental_data, BaseException):
            raise self.fundamental_data
        if callable(self.fundamental_data):
            return self.fundamental_data(symbol, indicator)
        return self.fundamental_data

    def stock_value_em(self, symbol: str) -> pd.DataFrame:
        self.calls.append(("stock_value_em", {"symbol": symbol}))
        if isinstance(self.valuation_data, BaseException):
            raise self.valuation_data
        if callable(self.valuation_data):
            return self.valuation_data(symbol)
        return self.valuation_data


class StockEvidenceFetchImmutabilityTests(unittest.TestCase):
    def test_frozen_and_errors_read_only(self):
        errors_map = {
            "fundamental": ERROR_MALFORMED_RESPONSE,
            "valuation": ERROR_NO_USABLE_VALUATION,
        }
        fetch = StockEvidenceFetch(
            symbol="600000",
            fundamental=None,
            valuation=None,
            errors=errors_map,
        )
        self.assertIsInstance(fetch.errors, MappingProxyType)
        with self.assertRaises(FrozenInstanceError):
            fetch.symbol = "000001"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            fetch.fundamental = None  # type: ignore[misc]
        with self.assertRaises(TypeError):
            fetch.errors["fundamental"] = "NEW_ERR"  # type: ignore[index]
        with self.assertRaises(TypeError):
            fetch.errors["valuation"] = "NEW_ERR"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            fetch.errors.pop("fundamental", None)  # type: ignore[attr-defined]

    def test_always_copies_errors_to_mapping_proxy(self):
        # Even when passed an existing MappingProxyType or dict, errors is independently copied
        raw_dict = {
            "fundamental": ERROR_MALFORMED_RESPONSE,
            "valuation": ERROR_NO_USABLE_VALUATION,
        }
        proxy = MappingProxyType(raw_dict)
        fetch = StockEvidenceFetch(
            symbol="600000",
            fundamental=None,
            valuation=None,
            errors=proxy,
        )
        # Mutating the underlying dict that backing the passed proxy must NOT affect fetch.errors
        raw_dict["fundamental"] = ERROR_NETWORK_UNAVAILABLE
        self.assertEqual(fetch.errors["fundamental"], ERROR_MALFORMED_RESPONSE)


class StockEvidenceFetchValidationTests(unittest.TestCase):
    def _dummy_fundamental(self, symbol: str = "600000") -> StockFundamentalObservation:
        t = _shanghai_dt(2024, 8, 25)
        return StockFundamentalObservation(
            symbol=symbol,
            report_period=date(2024, 6, 30),
            published_at=t,
            fetched_at=t,
            source="akshare",
            revenue_growth_pct=10.0,
            net_profit_growth_pct=10.0,
            roe_pct=10.0,
            eps=1.0,
        )

    def _dummy_valuation(self, symbol: str = "600000") -> StockValuationObservation:
        t = _shanghai_dt(2024, 8, 25)
        return StockValuationObservation(
            symbol=symbol,
            as_of=t,
            fetched_at=t,
            source="akshare",
            pe_ttm=10.0,
            pb=1.0,
            ps=1.0,
        )

    def test_symbol_strictly_validated(self):
        fund = self._dummy_fundamental("600000")
        val = self._dummy_valuation("600000")
        for invalid_sym in (None, 600000, "", "60000", "6000000", "ABCDEF", "60000A"):
            with self.subTest(symbol=invalid_sym):
                with self.assertRaises((ValueError, TypeError)):
                    StockEvidenceFetch(
                        symbol=invalid_sym,  # type: ignore[arg-type]
                        fundamental=fund,
                        valuation=val,
                    )

    def test_observation_types_strictly_validated(self):
        val = self._dummy_valuation("600000")
        fund = self._dummy_fundamental("600000")
        with self.assertRaises(TypeError):
            StockEvidenceFetch(
                symbol="600000",
                fundamental="invalid_type",  # type: ignore[arg-type]
                valuation=val,
            )
        with self.assertRaises(TypeError):
            StockEvidenceFetch(
                symbol="600000",
                fundamental=fund,
                valuation="invalid_type",  # type: ignore[arg-type]
            )

    def test_observation_symbol_match_strictly_validated(self):
        fund_other = self._dummy_fundamental("000001")
        val_match = self._dummy_valuation("600000")
        with self.assertRaises(ValueError):
            StockEvidenceFetch(
                symbol="600000",
                fundamental=fund_other,
                valuation=val_match,
            )

        fund_match = self._dummy_fundamental("600000")
        val_other = self._dummy_valuation("000001")
        with self.assertRaises(ValueError):
            StockEvidenceFetch(
                symbol="600000",
                fundamental=fund_match,
                valuation=val_other,
            )

    def test_errors_keys_and_values_strictly_validated(self):
        fund = self._dummy_fundamental("600000")
        # Invalid key
        with self.assertRaises(ValueError):
            StockEvidenceFetch(
                symbol="600000",
                fundamental=fund,
                valuation=None,
                errors={"valuation": ERROR_NO_USABLE_VALUATION, "unknown_key": ERROR_MALFORMED_RESPONSE},
            )
        # Invalid value
        with self.assertRaises(ValueError):
            StockEvidenceFetch(
                symbol="600000",
                fundamental=fund,
                valuation=None,
                errors={"valuation": "UNRECOGNIZED_CODE"},
            )

    def test_endpoint_cannot_have_both_observation_and_error(self):
        fund = self._dummy_fundamental("600000")
        val = self._dummy_valuation("600000")
        # fundamental has both
        with self.assertRaises(ValueError):
            StockEvidenceFetch(
                symbol="600000",
                fundamental=fund,
                valuation=val,
                errors={"fundamental": ERROR_MALFORMED_RESPONSE},
            )
        # valuation has both
        with self.assertRaises(ValueError):
            StockEvidenceFetch(
                symbol="600000",
                fundamental=fund,
                valuation=val,
                errors={"valuation": ERROR_MALFORMED_RESPONSE},
            )

    def test_missing_observation_must_have_error(self):
        fund = self._dummy_fundamental("600000")
        val = self._dummy_valuation("600000")
        # fundamental is None, but no fundamental error provided
        with self.assertRaises(ValueError):
            StockEvidenceFetch(
                symbol="600000",
                fundamental=None,
                valuation=val,
                errors={},
            )
        # valuation is None, but no valuation error provided
        with self.assertRaises(ValueError):
            StockEvidenceFetch(
                symbol="600000",
                fundamental=fund,
                valuation=None,
                errors={},
            )


class AkShareStockEvidenceProviderValidationTests(unittest.TestCase):
    def test_provider_max_rows_validation(self):
        self.assertEqual(AkShareStockEvidenceProvider()._max_rows, 4096)
        with self.assertRaises(ValueError):
            AkShareStockEvidenceProvider(max_rows=0)
        with self.assertRaises(ValueError):
            AkShareStockEvidenceProvider(max_rows=-10)
        with self.assertRaises(ValueError):
            AkShareStockEvidenceProvider(max_rows=4097)
        with self.assertRaises(TypeError):
            AkShareStockEvidenceProvider(max_rows=True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            AkShareStockEvidenceProvider(max_rows=10.5)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            AkShareStockEvidenceProvider(max_rows="512")  # type: ignore[arg-type]
        # Valid bounds
        p1 = AkShareStockEvidenceProvider(max_rows=1)
        self.assertEqual(p1._max_rows, 1)
        p4096 = AkShareStockEvidenceProvider(max_rows=4096)
        self.assertEqual(p4096._max_rows, 4096)

    def test_clock_rejects_naive_and_invalid_types(self):
        fake = FakeAkShareClient(
            fundamental_data=_valid_fundamental_df(),
            valuation_data=_valid_valuation_df(),
        )
        cutoff = _shanghai_dt(2026, 9, 16)
        # Rejects naive datetime, bool, int, float, str, date, None
        for invalid_clock in (
            lambda: datetime(2026, 9, 16, 12, 0),  # naive
            lambda: True,
            lambda: False,
            lambda: 1726470000,
            lambda: 1726470000.5,
            lambda: "2026-09-16 12:00:00",
            lambda: date(2026, 9, 16),
            lambda: None,
        ):
            with self.subTest(clock=invalid_clock):
                provider = AkShareStockEvidenceProvider(client=fake, clock=invalid_clock)
                with self.assertRaises((ValueError, TypeError)):
                    provider._get_fetched_at()

    def test_clock_does_not_alter_timezone(self):
        # Timezone-aware clock in UTC must be preserved, not altered to Shanghai
        utc_time = _utc_dt(2026, 9, 16, 12, 0)
        provider = AkShareStockEvidenceProvider(clock=lambda: utc_time)
        fetched = provider._get_fetched_at()
        self.assertEqual(fetched, utc_time)
        self.assertEqual(fetched.tzinfo, timezone.utc)

    def test_fetch_rejects_invalid_symbol(self):
        provider = AkShareStockEvidenceProvider(client=FakeAkShareClient())
        cutoff = _shanghai_dt(2026, 9, 16)
        for invalid_symbol in (None, 600000, "", "60000", "6000000", "60000A", "ABCDEF", "999999", "123456", "510300"):
            with self.subTest(symbol=invalid_symbol):
                with self.assertRaises((ValueError, TypeError)):
                    provider.fetch(symbol=invalid_symbol, cutoff=cutoff)  # type: ignore[arg-type]

    def test_fetch_rejects_invalid_cutoff(self):
        provider = AkShareStockEvidenceProvider(client=FakeAkShareClient())
        for invalid_cutoff in (None, "2026-09-16", date(2026, 9, 16), datetime(2026, 9, 16, 9, 30)):
            with self.subTest(cutoff=invalid_cutoff):
                with self.assertRaises((ValueError, TypeError)):
                    provider.fetch(symbol="600000", cutoff=invalid_cutoff)  # type: ignore[arg-type]

    def test_symbol_market_mapping(self):
        fake = FakeAkShareClient(
            fundamental_data=_valid_fundamental_df(symbol="600000", mapped_symbol="600000.SH"),
            valuation_data=_valid_valuation_df(),
        )
        clock_dt = _shanghai_dt(2026, 9, 16, 15, 30)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: clock_dt)
        cutoff = _shanghai_dt(2026, 9, 16, 15, 0)

        # Shanghai 600xxx
        provider.fetch("600000", cutoff)
        self.assertEqual(fake.calls[0], ("stock_financial_analysis_indicator_em", {"symbol": "600000.SH", "indicator": "按报告期"}))
        self.assertEqual(fake.calls[1], ("stock_value_em", {"symbol": "600000"}))

        # Shenzhen 000xxx
        fake.calls.clear()
        fake.fundamental_data = _valid_fundamental_df(symbol="000001", mapped_symbol="000001.SZ")
        provider.fetch("000001", cutoff)
        self.assertEqual(fake.calls[0], ("stock_financial_analysis_indicator_em", {"symbol": "000001.SZ", "indicator": "按报告期"}))
        self.assertEqual(fake.calls[1], ("stock_value_em", {"symbol": "000001"}))

        # Shenzhen ChiNext 300xxx
        fake.calls.clear()
        fake.fundamental_data = _valid_fundamental_df(symbol="300750", mapped_symbol="300750.SZ")
        provider.fetch("300750", cutoff)
        self.assertEqual(fake.calls[0], ("stock_financial_analysis_indicator_em", {"symbol": "300750.SZ", "indicator": "按报告期"}))
        self.assertEqual(fake.calls[1], ("stock_value_em", {"symbol": "300750"}))

        # Beijing Stock Exchange legacy and current code ranges
        for symbol in ("430047", "830799", "920002"):
            with self.subTest(symbol=symbol):
                fake.calls.clear()
                fake.fundamental_data = _valid_fundamental_df(
                    symbol=symbol, mapped_symbol=f"{symbol}.BJ",
                )
                provider.fetch(symbol, cutoff)
                self.assertEqual(
                    fake.calls[0],
                    ("stock_financial_analysis_indicator_em",
                     {"symbol": f"{symbol}.BJ", "indicator": "按报告期"}),
                )

    def test_delayed_import_when_client_is_none(self):
        # When client is None, provider does not import akshare in __init__
        provider = AkShareStockEvidenceProvider()
        self.assertIsNone(provider._client)


class AkShareStockEvidenceProviderSuccessTests(unittest.TestCase):
    def test_successful_fetch_both_endpoints(self):
        fake = FakeAkShareClient(
            fundamental_data=_valid_fundamental_df(
                symbol="600000",
                mapped_symbol="600000.SH",
                report_date="2024-06-30",
                notice_date="2024-08-25",
                revenue_growth=15.2,
                profit_growth=9.8,
                roe=11.4,
                eps=0.92,
            ),
            valuation_data=_valid_valuation_df(
                date_str="2024-08-25",
                pe_ttm=6.5,
                pb=0.62,
                ps=1.45,
            ),
        )
        clock_dt = _shanghai_dt(2026, 9, 16, 16, 0)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: clock_dt)
        cutoff = _shanghai_dt(2024, 8, 30, 0, 0)

        result = provider.fetch("600000", cutoff)
        self.assertEqual(result.symbol, "600000")
        self.assertEqual(len(result.errors), 0)

        fund = result.fundamental
        self.assertIsNotNone(fund)
        self.assertIsInstance(fund, StockFundamentalObservation)
        self.assertEqual(fund.symbol, "600000")
        self.assertEqual(fund.report_period, date(2024, 6, 30))
        self.assertEqual(fund.published_at, _shanghai_dt(2024, 8, 25))
        self.assertEqual(fund.fetched_at, clock_dt)
        self.assertEqual(fund.source, "akshare_eastmoney")
        self.assertEqual(fund.revenue_growth_pct, 15.2)
        self.assertEqual(fund.net_profit_growth_pct, 9.8)
        self.assertEqual(fund.roe_pct, 11.4)
        self.assertEqual(fund.eps, 0.92)

        val = result.valuation
        self.assertIsNotNone(val)
        self.assertIsInstance(val, StockValuationObservation)
        self.assertEqual(val.symbol, "600000")
        self.assertEqual(val.as_of, _shanghai_dt(2024, 8, 25))
        self.assertEqual(val.fetched_at, clock_dt)
        self.assertEqual(val.source, "akshare_eastmoney")
        self.assertEqual(val.pe_ttm, 6.5)
        self.assertEqual(val.pb, 0.62)
        self.assertEqual(val.ps, 1.45)

    def test_fetched_at_from_clock_called_after_and_later_than_cutoff(self):
        call_times = []

        def mock_clock():
            t = _shanghai_dt(2026, 9, 16, 17, 0)
            call_times.append(t)
            return t

        fake = FakeAkShareClient(
            fundamental_data=_valid_fundamental_df(),
            valuation_data=_valid_valuation_df(),
        )
        provider = AkShareStockEvidenceProvider(client=fake, clock=mock_clock)
        cutoff = _shanghai_dt(2024, 8, 30, 0, 0)

        result = provider.fetch("600000", cutoff)
        self.assertTrue(len(call_times) >= 1)
        self.assertGreater(result.fundamental.fetched_at, cutoff)
        self.assertGreater(result.valuation.fetched_at, cutoff)

    def test_utc_cutoff_comparison(self):
        # 2024-08-25 10:00:00 UTC == 2024-08-25 18:00:00 Shanghai
        utc_cutoff = _utc_dt(2024, 8, 25, 10, 0)
        clock_dt = _shanghai_dt(2026, 9, 16)
        fake = FakeAkShareClient(
            fundamental_data=_valid_fundamental_df(notice_date="2024-08-25"),
            valuation_data=_valid_valuation_df(date_str="2024-08-25"),
        )
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: clock_dt)
        result = provider.fetch("600000", utc_cutoff)

        self.assertIsNotNone(result.fundamental)
        self.assertIsNotNone(result.valuation)

    def test_accepts_negative_metrics(self):
        clock_dt = _shanghai_dt(2026, 9, 16)
        fake = FakeAkShareClient(
            fundamental_data=_valid_fundamental_df(
                revenue_growth=-5.5,
                profit_growth=-20.0,
                roe=-3.2,
                eps=-0.15,
            ),
            valuation_data=_valid_valuation_df(
                pe_ttm=-12.0,
                pb=-1.5,
                ps=0.8,
            ),
        )
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: clock_dt)
        result = provider.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertIsNotNone(result.fundamental)
        self.assertEqual(result.fundamental.revenue_growth_pct, -5.5)
        self.assertEqual(result.fundamental.eps, -0.15)
        self.assertIsNotNone(result.valuation)
        self.assertEqual(result.valuation.pe_ttm, -12.0)


class AkShareStockEvidenceProviderFilteringAndOrderTests(unittest.TestCase):
    def test_future_row_filtering(self):
        cutoff = _shanghai_dt(2024, 8, 20, 15, 0)
        clock_dt = _shanghai_dt(2026, 9, 16)

        fundamental_df = pd.DataFrame([
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-06-30",
                "NOTICE_DATE": "2024-08-25",  # Future relative to cutoff
                "TOTALOPERATEREVETZ": 99.9,
                "PARENTNETPROFITTZ": 99.9,
                "ROEJQ": 99.9,
                "EPSJB": 9.9,
            },
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-03-31",
                "NOTICE_DATE": "2024-04-28",  # Before cutoff -> should be selected
                "TOTALOPERATEREVETZ": 10.0,
                "PARENTNETPROFITTZ": 8.0,
                "ROEJQ": 9.0,
                "EPSJB": 0.5,
            },
        ])

        valuation_df = pd.DataFrame([
            {"数据日期": "2024-08-25", "PE(TTM)": 99.0, "市净率": 9.0, "市销率": 9.0},  # Future
            {"数据日期": "2024-08-19", "PE(TTM)": 5.0, "市净率": 0.5, "市销率": 1.0},  # Valid
        ])

        fake = FakeAkShareClient(fundamental_data=fundamental_df, valuation_data=valuation_df)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: clock_dt)
        result = provider.fetch("600000", cutoff)

        self.assertIsNotNone(result.fundamental)
        self.assertEqual(result.fundamental.report_period, date(2024, 3, 31))
        self.assertEqual(result.fundamental.published_at, _shanghai_dt(2024, 4, 28))

        self.assertIsNotNone(result.valuation)
        self.assertEqual(result.valuation.as_of, _shanghai_dt(2024, 8, 19))

    def test_out_of_order_rows_selects_latest(self):
        cutoff = _shanghai_dt(2024, 10, 1)
        clock_dt = _shanghai_dt(2026, 9, 16)

        # Shuffled rows
        fundamental_df = pd.DataFrame([
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2023-12-31",
                "NOTICE_DATE": "2024-03-15",
                "TOTALOPERATEREVETZ": 5.0,
                "PARENTNETPROFITTZ": 4.0,
                "ROEJQ": 7.0,
                "EPSJB": 0.4,
            },
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-06-30",
                "NOTICE_DATE": "2024-08-25",  # Latest valid
                "TOTALOPERATEREVETZ": 15.0,
                "PARENTNETPROFITTZ": 12.0,
                "ROEJQ": 11.0,
                "EPSJB": 0.9,
            },
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-03-31",
                "NOTICE_DATE": "2024-04-20",
                "TOTALOPERATEREVETZ": 8.0,
                "PARENTNETPROFITTZ": 6.0,
                "ROEJQ": 8.5,
                "EPSJB": 0.6,
            },
        ])

        valuation_df = pd.DataFrame([
            {"数据日期": "2024-08-10", "PE(TTM)": 5.1, "市净率": 0.51, "市销率": 1.1},
            {"数据日期": "2024-08-25", "PE(TTM)": 5.5, "市净率": 0.55, "市销率": 1.2},  # Latest valid
            {"数据日期": "2024-08-15", "PE(TTM)": 5.2, "市净率": 0.52, "市销率": 1.15},
        ])

        fake = FakeAkShareClient(fundamental_data=fundamental_df, valuation_data=valuation_df)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: clock_dt)
        result = provider.fetch("600000", cutoff)

        self.assertEqual(result.fundamental.published_at, _shanghai_dt(2024, 8, 25))
        self.assertEqual(result.fundamental.eps, 0.9)
        self.assertEqual(result.valuation.as_of, _shanghai_dt(2024, 8, 25))
        self.assertEqual(result.valuation.pe_ttm, 5.5)

    def test_no_usable_fundamental_and_valuation_when_all_future(self):
        cutoff = _shanghai_dt(2023, 1, 1)
        clock_dt = _shanghai_dt(2026, 9, 16)
        fake = FakeAkShareClient(
            fundamental_data=_valid_fundamental_df(notice_date="2024-08-25"),
            valuation_data=_valid_valuation_df(date_str="2024-08-25"),
        )
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: clock_dt)
        result = provider.fetch("600000", cutoff)

        self.assertIsNone(result.fundamental)
        self.assertIsNone(result.valuation)
        self.assertEqual(result.errors.get("fundamental"), ERROR_NO_USABLE_FUNDAMENTAL)
        self.assertEqual(result.errors.get("valuation"), ERROR_NO_USABLE_VALUATION)


class AkShareStockEvidenceProviderValidationAndMalformedTests(unittest.TestCase):
    def test_symbol_mismatch_fundamental(self):
        # Returned security code doesn't match requested symbol
        fake = FakeAkShareClient(
            fundamental_data=_valid_fundamental_df(symbol="000001", mapped_symbol="000001.SZ"),
            valuation_data=_valid_valuation_df(),
        )
        clock_dt = _shanghai_dt(2026, 9, 16)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: clock_dt)
        result = provider.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertIsNone(result.fundamental)
        self.assertEqual(result.errors.get("fundamental"), ERROR_SYMBOL_MISMATCH)
        # Valuation endpoint succeeds independently
        self.assertIsNotNone(result.valuation)

    def test_symbol_mismatch_valuation_if_column_present(self):
        val_df = _valid_valuation_df()
        val_df["SECURITY_CODE"] = "000001"
        fake = FakeAkShareClient(fundamental_data=_valid_fundamental_df(), valuation_data=val_df)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        result = provider.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertIsNotNone(result.fundamental)
        self.assertIsNone(result.valuation)
        self.assertEqual(result.errors.get("valuation"), ERROR_SYMBOL_MISMATCH)

    def test_duplicate_columns_rejected(self):
        # Fundamental with duplicate columns
        dup_fundamental = pd.DataFrame(
            [[ "600000.SH", "600000", "2024-06-30", "2024-08-25", "2024-08-25", 10.0, 10.0, 10.0, 1.0 ]],
            columns=["SECUCODE", "SECURITY_CODE", "REPORT_DATE", "NOTICE_DATE", "NOTICE_DATE", "TOTALOPERATEREVETZ", "PARENTNETPROFITTZ", "ROEJQ", "EPSJB"],
        )
        fake = FakeAkShareClient(fundamental_data=dup_fundamental, valuation_data=_valid_valuation_df())
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        result = provider.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertIsNone(result.fundamental)
        self.assertEqual(result.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)
        self.assertIsNotNone(result.valuation)

        # Valuation with duplicate columns
        dup_valuation = pd.DataFrame(
            [[ "2024-08-25", 5.0, 5.0, 0.5, 1.0 ]],
            columns=["数据日期", "PE(TTM)", "PE(TTM)", "市净率", "市销率"],
        )
        fake2 = FakeAkShareClient(fundamental_data=_valid_fundamental_df(), valuation_data=dup_valuation)
        provider2 = AkShareStockEvidenceProvider(client=fake2, clock=lambda: _shanghai_dt(2026, 9, 16))
        result2 = provider2.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertIsNotNone(result2.fundamental)
        self.assertIsNone(result2.valuation)
        self.assertEqual(result2.errors.get("valuation"), ERROR_MALFORMED_RESPONSE)

    def test_missing_required_columns_rejected(self):
        fund_df = _valid_fundamental_df().drop(columns=["EPSJB"])
        val_df = _valid_valuation_df().drop(columns=["市净率"])
        fake = FakeAkShareClient(fundamental_data=fund_df, valuation_data=val_df)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        result = provider.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertEqual(result.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)
        self.assertEqual(result.errors.get("valuation"), ERROR_MALFORMED_RESPONSE)

    def test_oversized_response_rejected(self):
        rows = [_valid_fundamental_df().iloc[0].to_dict() for _ in range(6)]
        oversized_df = pd.DataFrame(rows)
        fake = FakeAkShareClient(fundamental_data=oversized_df, valuation_data=_valid_valuation_df())
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16), max_rows=5)
        result = provider.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertIsNone(result.fundamental)
        self.assertEqual(result.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)

    def test_empty_response_rejected(self):
        fake = FakeAkShareClient(fundamental_data=pd.DataFrame(), valuation_data=None)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        result = provider.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertEqual(result.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)
        self.assertEqual(result.errors.get("valuation"), ERROR_MALFORMED_RESPONSE)

    def test_malformed_values_rejected(self):
        # bool in numeric field
        df_bool = pd.DataFrame([{
            "SECUCODE": "600000.SH",
            "SECURITY_CODE": "600000",
            "REPORT_DATE": "2024-06-30",
            "NOTICE_DATE": "2024-08-25",
            "TOTALOPERATEREVETZ": 10.0,
            "PARENTNETPROFITTZ": 10.0,
            "ROEJQ": 10.0,
            "EPSJB": True,
        }])
        fake = FakeAkShareClient(fundamental_data=df_bool, valuation_data=_valid_valuation_df())
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))
        self.assertEqual(res.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)

        # NaN in required field
        df_nan = _valid_fundamental_df()
        df_nan.at[0, "ROEJQ"] = float("nan")
        fake.fundamental_data = df_nan
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))
        self.assertEqual(res.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)

        # Infinity in required field
        df_inf = _valid_valuation_df()
        df_inf.at[0, "PE(TTM)"] = float("inf")
        fake.fundamental_data = _valid_fundamental_df()
        fake.valuation_data = df_inf
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))
        self.assertEqual(res.errors.get("valuation"), ERROR_MALFORMED_RESPONSE)

        # Empty field
        df_empty = _valid_fundamental_df()
        df_empty.at[0, "SECURITY_CODE"] = "  "
        fake.fundamental_data = df_empty
        fake.valuation_data = _valid_valuation_df()
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))
        self.assertEqual(res.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)

        # Overlong string
        df_long = _valid_fundamental_df()
        df_long.at[0, "SECUCODE"] = "600000.SH" + "X" * 300
        fake.fundamental_data = df_long
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))
        self.assertEqual(res.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)

        # Bad date
        df_baddate = _valid_fundamental_df()
        df_baddate.at[0, "NOTICE_DATE"] = "not-a-date"
        fake.fundamental_data = df_baddate
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))
        self.assertEqual(res.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)


class AkShareStockEvidenceProviderErrorIsolationTests(unittest.TestCase):
    def test_network_unavailable_mapped_and_never_leaked(self):
        secret = "SECRET_TOKEN_PASS_9999"
        net_err = ConnectionError(f"Connection to https://internal-api.com/?key={secret} timed out")
        fake = FakeAkShareClient(fundamental_data=net_err, valuation_data=_valid_valuation_df())
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertIsNone(res.fundamental)
        self.assertEqual(res.errors.get("fundamental"), ERROR_NETWORK_UNAVAILABLE)
        for k, v in res.errors.items():
            self.assertNotIn(secret, k)
            self.assertNotIn(secret, v)
        self.assertIsNotNone(res.valuation)

    def test_exception_getter_and_call_isolated(self):
        class BrokenGetterClient:
            @property
            def stock_financial_analysis_indicator_em(self):
                raise RuntimeError("Getter failure for indicator endpoint")

            def stock_value_em(self, symbol: str):
                return _valid_valuation_df()

        provider = AkShareStockEvidenceProvider(client=BrokenGetterClient(), clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertIsNone(res.fundamental)
        self.assertEqual(res.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)
        self.assertIsNotNone(res.valuation)

    def test_call_exception_isolated(self):
        secret = "DB_PASSWORD_12345"
        call_err = RuntimeError(f"Internal computation error with {secret}")
        fake = FakeAkShareClient(fundamental_data=_valid_fundamental_df(), valuation_data=call_err)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertIsNotNone(res.fundamental)
        self.assertIsNone(res.valuation)
        self.assertEqual(res.errors.get("valuation"), ERROR_MALFORMED_RESPONSE)
        for k, v in res.errors.items():
            self.assertNotIn(secret, k)
            self.assertNotIn(secret, v)


class AkShareStockEvidenceProviderExecutionTests(unittest.TestCase):
    def test_fetch_propagates_keyboard_interrupt(self):
        fake = FakeAkShareClient(fundamental_data=KeyboardInterrupt("Interrupted by user"))
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        with self.assertRaises(KeyboardInterrupt):
            provider.fetch("600000", _shanghai_dt(2026, 1, 1))

    def test_fetch_propagates_system_exit(self):
        fake = FakeAkShareClient(fundamental_data=SystemExit(1))
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        with self.assertRaises(SystemExit):
            provider.fetch("600000", _shanghai_dt(2026, 1, 1))

    def test_fetch_catches_memory_error(self):
        fake = FakeAkShareClient(fundamental_data=MemoryError("Out of memory"), valuation_data=_valid_valuation_df())
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))
        self.assertIsNone(res.fundamental)
        self.assertEqual(res.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)
        self.assertIsNotNone(res.valuation)


class AkShareStockEvidenceProviderDataFrameProcessingTests(unittest.TestCase):
    def test_older_row_bad_metrics_do_not_pollute_latest_row_fundamental(self):
        cutoff = _shanghai_dt(2024, 9, 1)
        clock_dt = _shanghai_dt(2026, 9, 16)
        # Latest row is valid, older row has bad metric (NaN)
        fundamental_df = pd.DataFrame([
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-06-30",
                "NOTICE_DATE": "2024-08-25",  # Latest valid
                "TOTALOPERATEREVETZ": 12.0,
                "PARENTNETPROFITTZ": 10.0,
                "ROEJQ": 11.0,
                "EPSJB": 0.8,
            },
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-03-31",
                "NOTICE_DATE": "2024-04-20",  # Older row with NaN metric
                "TOTALOPERATEREVETZ": float("nan"),
                "PARENTNETPROFITTZ": 5.0,
                "ROEJQ": 5.0,
                "EPSJB": 0.3,
            },
        ])
        fake = FakeAkShareClient(fundamental_data=fundamental_df, valuation_data=_valid_valuation_df())
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: clock_dt)
        res = provider.fetch("600000", cutoff)
        self.assertIsNotNone(res.fundamental)
        self.assertEqual(res.fundamental.published_at, _shanghai_dt(2024, 8, 25))
        self.assertEqual(res.fundamental.eps, 0.8)

    def test_future_row_bad_metrics_do_not_pollute_latest_row_fundamental(self):
        cutoff = _shanghai_dt(2024, 5, 1)
        clock_dt = _shanghai_dt(2026, 9, 16)
        # Future row has bad metric (NaN), valid row within cutoff is valid
        fundamental_df = pd.DataFrame([
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-06-30",
                "NOTICE_DATE": "2024-08-25",  # Future relative to cutoff, bad metric
                "TOTALOPERATEREVETZ": "NOT_A_NUMBER",
                "PARENTNETPROFITTZ": 10.0,
                "ROEJQ": 11.0,
                "EPSJB": 0.8,
            },
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-03-31",
                "NOTICE_DATE": "2024-04-20",  # Valid row within cutoff
                "TOTALOPERATEREVETZ": 10.0,
                "PARENTNETPROFITTZ": 8.0,
                "ROEJQ": 9.0,
                "EPSJB": 0.5,
            },
        ])
        fake = FakeAkShareClient(fundamental_data=fundamental_df, valuation_data=_valid_valuation_df())
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: clock_dt)
        res = provider.fetch("600000", cutoff)
        self.assertIsNotNone(res.fundamental)
        self.assertEqual(res.fundamental.published_at, _shanghai_dt(2024, 4, 20))

    def test_latest_available_row_bad_metrics_results_in_malformed_no_fallback_fundamental(self):
        cutoff = _shanghai_dt(2024, 9, 1)
        clock_dt = _shanghai_dt(2026, 9, 16)
        # Latest row has NaN metric, older row has valid metric
        # Must return ERROR_MALFORMED_RESPONSE and NOT fall back to older row!
        fundamental_df = pd.DataFrame([
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-06-30",
                "NOTICE_DATE": "2024-08-25",  # Latest row, but bad metric
                "TOTALOPERATEREVETZ": float("nan"),
                "PARENTNETPROFITTZ": 10.0,
                "ROEJQ": 11.0,
                "EPSJB": 0.8,
            },
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-03-31",
                "NOTICE_DATE": "2024-04-20",  # Older valid row
                "TOTALOPERATEREVETZ": 8.0,
                "PARENTNETPROFITTZ": 6.0,
                "ROEJQ": 7.0,
                "EPSJB": 0.4,
            },
        ])
        fake = FakeAkShareClient(fundamental_data=fundamental_df, valuation_data=_valid_valuation_df())
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: clock_dt)
        res = provider.fetch("600000", cutoff)
        self.assertIsNone(res.fundamental)
        self.assertEqual(res.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)

    def test_older_row_bad_metrics_do_not_pollute_latest_row_valuation(self):
        cutoff = _shanghai_dt(2024, 8, 30)
        valuation_df = pd.DataFrame([
            {"数据日期": "2024-08-25", "PE(TTM)": 6.0, "市净率": 0.6, "市销率": 1.1},  # Latest valid
            {"数据日期": "2024-08-10", "PE(TTM)": float("nan"), "市净率": 0.5, "市销率": 1.0},  # Older row with NaN
        ])
        fake = FakeAkShareClient(fundamental_data=_valid_fundamental_df(), valuation_data=valuation_df)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", cutoff)
        self.assertIsNotNone(res.valuation)
        self.assertEqual(res.valuation.as_of, _shanghai_dt(2024, 8, 25))

    def test_future_row_bad_metrics_do_not_pollute_latest_row_valuation(self):
        cutoff = _shanghai_dt(2024, 8, 20)
        valuation_df = pd.DataFrame([
            {"数据日期": "2024-08-25", "PE(TTM)": "BAD_VAL", "市净率": 0.6, "市销率": 1.1},  # Future bad
            {"数据日期": "2024-08-10", "PE(TTM)": 5.2, "市净率": 0.5, "市销率": 1.0},  # Valid
        ])
        fake = FakeAkShareClient(fundamental_data=_valid_fundamental_df(), valuation_data=valuation_df)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", cutoff)
        self.assertIsNotNone(res.valuation)
        self.assertEqual(res.valuation.as_of, _shanghai_dt(2024, 8, 10))

    def test_latest_available_row_bad_metrics_results_in_malformed_no_fallback_valuation(self):
        cutoff = _shanghai_dt(2024, 8, 30)
        valuation_df = pd.DataFrame([
            {"数据日期": "2024-08-25", "PE(TTM)": float("nan"), "市净率": 0.6, "市销率": 1.1},  # Latest bad
            {"数据日期": "2024-08-10", "PE(TTM)": 5.2, "市净率": 0.5, "市销率": 1.0},  # Older valid
        ])
        fake = FakeAkShareClient(fundamental_data=_valid_fundamental_df(), valuation_data=valuation_df)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", cutoff)
        self.assertIsNone(res.valuation)
        self.assertEqual(res.errors.get("valuation"), ERROR_MALFORMED_RESPONSE)

    def test_symbol_mismatch_in_any_row_fails_immediately(self):
        cutoff = _shanghai_dt(2024, 9, 1)
        fundamental_df = pd.DataFrame([
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-06-30",
                "NOTICE_DATE": "2024-08-25",
                "TOTALOPERATEREVETZ": 12.0,
                "PARENTNETPROFITTZ": 10.0,
                "ROEJQ": 11.0,
                "EPSJB": 0.8,
            },
            {
                "SECUCODE": "000001.SZ",
                "SECURITY_CODE": "000001",  # Mismatch in another row
                "REPORT_DATE": "2024-03-31",
                "NOTICE_DATE": "2024-04-20",
                "TOTALOPERATEREVETZ": 8.0,
                "PARENTNETPROFITTZ": 6.0,
                "ROEJQ": 7.0,
                "EPSJB": 0.4,
            },
        ])
        fake = FakeAkShareClient(fundamental_data=fundamental_df, valuation_data=_valid_valuation_df())
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", cutoff)
        self.assertIsNone(res.fundamental)
        self.assertEqual(res.errors.get("fundamental"), ERROR_SYMBOL_MISMATCH)

    def test_bad_date_in_any_row_fails_immediately(self):
        cutoff = _shanghai_dt(2024, 9, 1)
        fundamental_df = pd.DataFrame([
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-06-30",
                "NOTICE_DATE": "2024-08-25",
                "TOTALOPERATEREVETZ": 12.0,
                "PARENTNETPROFITTZ": 10.0,
                "ROEJQ": 11.0,
                "EPSJB": 0.8,
            },
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "bad-date-format",  # Bad date format in older row
                "NOTICE_DATE": "2024-04-20",
                "TOTALOPERATEREVETZ": 8.0,
                "PARENTNETPROFITTZ": 6.0,
                "ROEJQ": 7.0,
                "EPSJB": 0.4,
            },
        ])
        fake = FakeAkShareClient(fundamental_data=fundamental_df, valuation_data=_valid_valuation_df())
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", cutoff)
        self.assertIsNone(res.fundamental)
        self.assertEqual(res.errors.get("fundamental"), ERROR_MALFORMED_RESPONSE)


class AkShareStockEvidenceProviderDecimalPrecisionTests(unittest.TestCase):
    def test_numeric_strings_parsed_as_decimal_exact(self):
        fundamental_df = pd.DataFrame([
            {
                "SECUCODE": "600000.SH",
                "SECURITY_CODE": "600000",
                "REPORT_DATE": "2024-06-30",
                "NOTICE_DATE": "2024-08-25",
                "TOTALOPERATEREVETZ": "12.345678901234567890",
                "PARENTNETPROFITTZ": "8.12345678901234567890",
                "ROEJQ": "10.5",
                "EPSJB": "0.85",
            }
        ])
        valuation_df = pd.DataFrame([
            {
                "数据日期": "2024-08-25",
                "PE(TTM)": "5.67890123456789012345",
                "市净率": "0.55555555555555555555",
                "市销率": "1.23456789012345678901",
            }
        ])
        fake = FakeAkShareClient(fundamental_data=fundamental_df, valuation_data=valuation_df)
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))

        self.assertIsNotNone(res.fundamental)
        self.assertIsInstance(res.fundamental.revenue_growth_pct, Decimal)
        self.assertEqual(res.fundamental.revenue_growth_pct, Decimal("12.345678901234567890"))
        self.assertIsInstance(res.fundamental.eps, Decimal)
        self.assertEqual(res.fundamental.eps, Decimal("0.85"))

        self.assertIsNotNone(res.valuation)
        self.assertIsInstance(res.valuation.pe_ttm, Decimal)
        self.assertEqual(res.valuation.pe_ttm, Decimal("5.67890123456789012345"))
        self.assertIsInstance(res.valuation.pb, Decimal)
        self.assertEqual(res.valuation.pb, Decimal("0.55555555555555555555"))

    def test_non_finite_numeric_strings_rejected(self):
        for bad_str in ("NaN", "nan", "Infinity", "-Infinity", "inf", "-inf"):
            with self.subTest(val=bad_str):
                val_df = pd.DataFrame([
                    {"数据日期": "2024-08-25", "PE(TTM)": bad_str, "市净率": "0.5", "市销率": "1.0"}
                ])
                fake = FakeAkShareClient(fundamental_data=_valid_fundamental_df(), valuation_data=val_df)
                provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
                res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))
                self.assertIsNone(res.valuation)
                self.assertEqual(res.errors.get("valuation"), ERROR_MALFORMED_RESPONSE)


class AkShareStockEvidenceProviderSecretLeakAndDegradationTests(unittest.TestCase):
    def test_private_parse_errors_do_not_contain_raw_string(self):
        secret = "SUPER_SECRET_VALUE_987654321"
        # 1. _parse_numeric
        with self.assertRaises(ValueError) as ctx:
            _parse_numeric(f"not-a-number-{secret}", "test_metric")
        self.assertNotIn(secret, str(ctx.exception))

        # 2. _parse_date
        with self.assertRaises(ValueError) as ctx:
            _parse_date(f"not-a-date-{secret}", "test_date")
        self.assertNotIn(secret, str(ctx.exception))

        # 3. _parse_shanghai_datetime
        with self.assertRaises(ValueError) as ctx:
            _parse_shanghai_datetime(f"not-a-dt-{secret}", "test_dt")
        self.assertNotIn(secret, str(ctx.exception))

        # 4. _parse_str
        with self.assertRaises(ValueError) as ctx:
            _parse_str("   ", "test_str")
        self.assertNotIn("   ", str(ctx.exception))

    def test_independent_endpoint_degradation_fundamental_down_valuation_up(self):
        fake = FakeAkShareClient(
            fundamental_data=ConnectionError("Fundamental timeout"),
            valuation_data=_valid_valuation_df(),
        )
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))
        self.assertIsNone(res.fundamental)
        self.assertEqual(res.errors.get("fundamental"), ERROR_NETWORK_UNAVAILABLE)
        self.assertIsNotNone(res.valuation)
        self.assertNotIn("valuation", res.errors)

    def test_independent_endpoint_degradation_valuation_down_fundamental_up(self):
        fake = FakeAkShareClient(
            fundamental_data=_valid_fundamental_df(),
            valuation_data=ConnectionError("Valuation timeout"),
        )
        provider = AkShareStockEvidenceProvider(client=fake, clock=lambda: _shanghai_dt(2026, 9, 16))
        res = provider.fetch("600000", _shanghai_dt(2026, 1, 1))
        self.assertIsNotNone(res.fundamental)
        self.assertNotIn("fundamental", res.errors)
        self.assertIsNone(res.valuation)
        self.assertEqual(res.errors.get("valuation"), ERROR_NETWORK_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
