import unittest
from unittest.mock import patch

from app.chan.models import KLine
from app.market.hk_index import AkShareHKIndexProvider
from app.market.models import Quote


class _Frame:
    def __init__(self, records):
        self.records = records

    def to_dict(self, orient):
        assert orient == "records"
        return list(self.records)


class _FakeAkShare:
    def __init__(self):
        self.calls = []

    def stock_hk_index_spot_sina(self):
        self.calls.append(("spot", {}))
        return _Frame([
            {"代码": "HSI", "名称": "恒生指数", "最新价": 18200.0, "涨跌幅": 0.1},
            {"代码": "HSTECH", "名称": "恒生科技指数", "最新价": 3788.12, "涨跌幅": -0.55},
        ])

    def stock_hk_index_daily_sina(self, **kwargs):
        self.calls.append(("daily", kwargs))
        return _Frame([
            {"date": "2026-09-11", "open": 3780, "high": 3800, "low": 3750, "close": 3788, "volume": 1200},
            {"date": "2026-09-10", "open": 3700, "high": 3760, "low": 3680, "close": 3750, "volume": 1000},
        ])


class HKIndexProviderTests(unittest.TestCase):
    def test_fetch_selects_hstech_spot_quote(self):
        fake = _FakeAkShare()
        provider = AkShareHKIndexProvider(ak_module=fake)

        quote = provider.fetch("HK.HSTECH")

        self.assertEqual(quote.code, "HK.HSTECH")
        self.assertEqual(quote.name, "恒生科技指数")
        self.assertEqual(quote.price, 3788.12)
        self.assertEqual(quote.change, -0.55)
        self.assertEqual(quote.source, "akshare_hk_index")
        self.assertEqual(fake.calls, [("spot", {})])

    def test_fetch_klines_maps_and_sorts_hstech_history(self):
        fake = _FakeAkShare()
        provider = AkShareHKIndexProvider(ak_module=fake)

        lines = provider.fetch_klines("HSTECH.HK")

        self.assertEqual(
            lines,
            [
                KLine("2026-09-10", 3700, 3760, 3680, 3750, 1000),
                KLine("2026-09-11", 3780, 3800, 3750, 3788, 1200),
            ],
        )
        self.assertEqual(fake.calls[-1], ("daily", {"symbol": "HSTECH"}))

    def test_rejects_other_symbols_and_invalid_ohlcv(self):
        provider = AkShareHKIndexProvider(ak_module=_FakeAkShare())
        with self.assertRaises(ValueError):
            provider.fetch("HK.HSI")

        class InvalidAkShare(_FakeAkShare):
            def stock_hk_index_daily_sina(self, **kwargs):
                return _Frame([{
                    "date": "2026-09-11", "open": 1, "high": 1, "low": 2,
                    "close": 1, "volume": 1,
                }])

        with self.assertRaises(ValueError):
            AkShareHKIndexProvider(ak_module=InvalidAkShare()).fetch_klines("HSTECH")

    def test_normalizes_oversized_spot_numeric_to_value_error(self):
        class OversizedAkShare(_FakeAkShare):
            def stock_hk_index_spot_sina(self):
                return _Frame([{
                    "代码": "HSTECH", "名称": "恒生科技指数",
                    "最新价": 10 ** 1000, "涨跌幅": 1.0,
                }])

        with self.assertRaises(ValueError):
            AkShareHKIndexProvider(ak_module=OversizedAkShare()).fetch("HSTECH")

    def test_rejects_zero_or_negative_spot_and_history_values(self):
        class InvalidSpotAkShare(_FakeAkShare):
            def __init__(self, price):
                super().__init__()
                self.price = price

            def stock_hk_index_spot_sina(self):
                return _Frame([{
                    "代码": "HSTECH", "名称": "恒生科技指数",
                    "最新价": self.price, "涨跌幅": 1.0,
                }])

        for price in (0, -1):
            with self.subTest(price=price), self.assertRaises(ValueError):
                AkShareHKIndexProvider(ak_module=InvalidSpotAkShare(price)).fetch("HSTECH")

        class InvalidHistoryAkShare(_FakeAkShare):
            def __init__(self, values):
                super().__init__()
                self.values = values

            def stock_hk_index_daily_sina(self, **kwargs):
                return _Frame([dict(
                    date="2026-09-11", open=self.values[0], high=self.values[1],
                    low=self.values[2], close=self.values[3], volume=self.values[4],
                )])

        for values in (
            (0, 1, 0.5, 0.8, 1),
            (-1, 1, 0.5, 0.8, 1),
            (1, 1, 0.5, 0.8, -1),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                AkShareHKIndexProvider(ak_module=InvalidHistoryAkShare(values)).fetch_klines("HSTECH")

    def test_date_parser_rejects_trailing_garbage(self):
        class InvalidDateAkShare(_FakeAkShare):
            def stock_hk_index_daily_sina(self, **kwargs):
                return _Frame([{
                    "date": "2026-09-11 trailing", "open": 1, "high": 2,
                    "low": 1, "close": 1.5, "volume": 1,
                }])

        with self.assertRaises(ValueError):
            AkShareHKIndexProvider(ak_module=InvalidDateAkShare()).fetch_klines("HSTECH")

    def test_spot_result_uses_injected_clock_ttl(self):
        fake = _FakeAkShare()
        current = [100.0]
        provider = AkShareHKIndexProvider(ak_module=fake, clock=lambda: current[0])

        provider.fetch("HK.HSTECH")
        provider.fetch("HSTECH")
        current[0] = 115.0
        provider.fetch("HSTECH.HK")
        current[0] = 116.0
        provider.fetch("HK.HSTECH")

        self.assertEqual([call[0] for call in fake.calls], ["spot", "spot"])

    def test_fetch_imports_akshare_lazily(self):
        fake = _FakeAkShare()
        with patch("app.market.hk_index.importlib.import_module", return_value=fake) as importer:
            AkShareHKIndexProvider().fetch("HSTECH")

        importer.assert_called_once_with("akshare")


if __name__ == "__main__":
    unittest.main()
