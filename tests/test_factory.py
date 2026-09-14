import os
import unittest
from unittest.mock import patch

from app.chan.models import KLine
from app.market.akshare import AkShareProvider
from app.market.factory import create_default_collector, create_portfolio_valuation_router
from app.market.fund_nav import AkShareFundNavProvider
from app.market.hk_index import AkShareHKIndexProvider
from app.market.collector import MarketCollector
from app.market.models import Quote
from app.portfolio.valuation import PortfolioValuationRouter
from app.market.sina import SinaProvider
from app.market.tushare import TushareProvider


class FactoryTests(unittest.TestCase):
    def test_default_collector_uses_akshare_without_optional_token(self):
        with patch.dict(os.environ, {}, clear=True):
            collector = create_default_collector()

        self.assertIsInstance(collector.primary, AkShareProvider)
        self.assertIsInstance(collector.fallback, list)
        self.assertEqual(
            [type(provider).__name__ for provider in collector.fallback],
            ["SinaProvider", "TencentHistoryProvider"],
        )

    def test_default_collector_enables_tushare_when_token_is_configured(self):
        with patch.dict(os.environ, {"TUSHARE_TOKEN": "secret-token"}, clear=True):
            collector = create_default_collector()

        self.assertIsInstance(collector.primary, AkShareProvider)
        self.assertIsInstance(collector.fallback, list)
        self.assertEqual(
            [type(provider).__name__ for provider in collector.fallback],
            ["SinaProvider", "TencentHistoryProvider", "TushareProvider"],
        )
        self.assertEqual(collector.fallback[2].token, "secret-token")

    def test_history_fallback_reaches_tencent_after_primary_and_sina_fail(self):
        with patch.dict(os.environ, {}, clear=True):
            collector = create_default_collector()
        primary, sina, tencent = collector.primary, collector.fallback[0], collector.fallback[1]
        lines = [
            KLine("2026-09-10", 9.8, 10.1, 9.7, 10.0, 1000.0),
            KLine("2026-09-11", 10.0, 10.5, 9.9, 10.4, 2000.0),
        ]

        with patch.object(AkShareProvider, "fetch_klines", side_effect=RuntimeError("primary unavailable")), \
                patch.object(SinaProvider, "fetch_klines", create=True, side_effect=RuntimeError("sina unavailable")), \
                patch.object(type(tencent), "fetch_klines", return_value=lines) as tencent_history, \
                patch.object(type(tencent), "fetch", create=True, side_effect=AssertionError("quote path must not use Tencent")):
            result = collector.get_klines(
                "600660", start_date="20260901", end_date="20260913"
            )

        self.assertEqual(result, lines)
        tencent_history.assert_called_once_with(
            "600660", start_date="20260901", end_date="20260913"
        )

    def test_quote_fallback_still_uses_sina_and_never_tencent(self):
        with patch.dict(os.environ, {}, clear=True):
            collector = create_default_collector()
        quote = Quote("600660", "福耀玻璃", 54.0, 1.2, "2026-09-11 15:00:00")
        sina = collector.fallback[0]
        tencent = collector.fallback[1]

        with patch.object(AkShareProvider, "fetch", side_effect=RuntimeError("primary unavailable")), \
                patch.object(type(sina), "fetch", return_value=quote) as sina_quote, \
                patch.object(type(tencent), "fetch", create=True, side_effect=AssertionError("quote path must not use Tencent")):
            result = collector.get_quote("600660")

        self.assertEqual(result, quote)
        sina_quote.assert_called_once_with("600660")

    def test_portfolio_router_wires_providers_without_network_calls(self):
        with patch.object(AkShareProvider, "fetch") as exchange_fetch, \
                patch.object(AkShareFundNavProvider, "fetch") as nav_fetch, \
                patch.object(AkShareHKIndexProvider, "fetch") as hk_fetch:
            router = create_portfolio_valuation_router()

        self.assertIsInstance(router, PortfolioValuationRouter)
        self.assertIsInstance(router.exchange_collector, MarketCollector)
        self.assertIsInstance(router.nav_provider, AkShareFundNavProvider)
        self.assertIsInstance(router.hk_provider, AkShareHKIndexProvider)
        exchange_fetch.assert_not_called()
        nav_fetch.assert_not_called()
        hk_fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
