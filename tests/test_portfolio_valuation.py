import unittest
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.market.models import Quote
from app.portfolio.models import (
    AccountConfig,
    HoldingConfig,
    PortfolioConfig,
)
from app.portfolio.valuation import PortfolioValuationRouter


class _Provider:
    def __init__(self, quotes=None, error_codes=()):
        self.quotes = quotes or {}
        self.error_codes = set(error_codes)
        self.calls = []

    def fetch(self, code):
        self.calls.append(code)
        if code in self.error_codes:
            raise RuntimeError(f"offline: {code}")
        return self.quotes[code]


class _ExchangeCollector(_Provider):
    get_quote = _Provider.fetch


def _holding(code, valuation_mode):
    return HoldingConfig(
        code=code,
        name=code,
        market="CN" if valuation_mode == "exchange" else "GLOBAL",
        instrument_type="fund",
        valuation_mode=valuation_mode,
        cost_price=Decimal("1"),
        baseline_value=Decimal("100"),
        sector="test",
        theme="test",
    )


def _config(*holdings):
    account = AccountConfig(
        account_id="A",
        name="A",
        strategy="test",
        baseline_date=datetime(2026, 9, 11).date(),
        total_assets=Decimal("1000"),
        cash=Decimal("0"),
        holdings=holdings,
    )
    return PortfolioConfig(schema_version=2, accounts=(account,))


class PortfolioValuationRouterTests(unittest.TestCase):
    NOW = datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc)

    def test_routes_exchange_nav_and_hstech_to_their_providers(self):
        exchange = _ExchangeCollector({
            "AAA": Quote("AAA", "AAA", 10, 1, "2026-09-11T03:55:00+00:00"),
        })
        nav = _Provider({
            "016496": Quote("016496", "016496", 1.111, 1, "2026-09-10", "nav", "2026-09-10"),
        })
        hk = _Provider({
            "HK.HSTECH": Quote("HK.HSTECH", "HSTECH", 3788, -0.5, "2026-09-11T03:55:00+00:00", "hk"),
        })
        router = PortfolioValuationRouter(exchange, nav, hk, clock=lambda: self.NOW)

        result = router.value_portfolio(
            _config(_holding("AAA", "exchange"), _holding("016496", "fund_nav")),
            include_hstech=True,
        )

        self.assertEqual(exchange.calls, ["AAA"])
        self.assertEqual(nav.calls, ["016496"])
        self.assertEqual(hk.calls, ["HK.HSTECH"])
        self.assertEqual(result["AAA"].price, Decimal("10"))
        self.assertEqual(result["016496"].price, Decimal("1.111"))
        self.assertEqual(result["HK.HSTECH"].price, Decimal("3788"))

    def test_exchange_history_uses_collector_public_get_klines(self):
        class HistoryCollector:
            def __init__(self):
                self.calls = []

            def get_klines(self, code, **kwargs):
                self.calls.append((code, kwargs))
                return ["history"]

        exchange = HistoryCollector()
        router = PortfolioValuationRouter(exchange, _Provider(), _Provider())

        result = router.get_klines_for_holding(
            _holding("AAA", "exchange"), period="daily"
        )

        self.assertEqual(result, ["history"])
        self.assertEqual(exchange.calls, [("AAA", {"period": "daily"})])

    def test_routes_hstech_alias_holding_to_hk_provider_and_canonicalizes_it(self):
        hk = _Provider({
            "HK.HSTECH": Quote(
                "HK.HSTECH", "HSTECH", 3788, -0.5,
                "2026-09-11T03:55:00+00:00", "hk",
            ),
        })
        exchange = _ExchangeCollector(error_codes={"HSTECH"})
        router = PortfolioValuationRouter(exchange, _Provider(), hk, clock=lambda: self.NOW)

        result = router.value_portfolio(
            _config(_holding("HSTECH", "fund_nav")), include_hstech=False
        )

        self.assertEqual(hk.calls, ["HK.HSTECH"])
        self.assertEqual(exchange.calls, [])
        self.assertEqual(set(result), {"HK.HSTECH"})
        self.assertEqual(result["HK.HSTECH"].code, "HK.HSTECH")

    def test_isolates_failed_holding_and_preserves_successful_valuations(self):
        exchange = _ExchangeCollector(
            {"AAA": Quote("AAA", "AAA", 10, 1, "2026-09-11T03:55:00+00:00")},
            error_codes={"BROKEN"},
        )
        router = PortfolioValuationRouter(
            exchange,
            _Provider(),
            _Provider(),
            clock=lambda: self.NOW,
        )

        result = router.value_portfolio(
            _config(_holding("AAA", "exchange"), _holding("BROKEN", "exchange")),
            include_hstech=False,
        )

        self.assertEqual(result["AAA"].price, Decimal("10"))
        self.assertEqual(result["BROKEN"].price, Decimal("0"))
        self.assertEqual(result["BROKEN"].freshness, "failed")
        self.assertEqual(result["BROKEN"].error, "offline: BROKEN")

    def test_marks_old_exchange_quote_stale_during_market_hours(self):
        exchange = _ExchangeCollector({
            "AAA": Quote("AAA", "AAA", 10, 1, "2026-09-11T02:30:00+00:00"),
        })
        active_session_now = datetime(2026, 9, 11, 3, 0, tzinfo=timezone.utc)
        router = PortfolioValuationRouter(
            exchange, _Provider(), _Provider(), clock=lambda: active_session_now
        )

        valuation = router.value_holding(_holding("AAA", "exchange"))

        self.assertEqual(valuation.freshness, "stale")

    def test_naive_cn_timestamp_uses_shanghai_time_at_exact_boundary(self):
        holding = _holding("AAA", "exchange")
        quote = Quote("AAA", "AAA", 10, 1, "2026-09-11 09:30:00")

        exact = PortfolioValuationRouter(
            _ExchangeCollector({"AAA": quote}), _Provider(), _Provider(),
            clock=lambda: datetime(2026, 9, 11, 1, 45, tzinfo=timezone.utc),
        ).value_holding(holding)
        late = PortfolioValuationRouter(
            _ExchangeCollector({"AAA": quote}), _Provider(), _Provider(),
            clock=lambda: datetime(2026, 9, 11, 1, 46, tzinfo=timezone.utc),
        ).value_holding(holding)

        self.assertEqual(exact.as_of.utcoffset(), ZoneInfo("Asia/Shanghai").utcoffset(exact.as_of))
        self.assertEqual(exact.freshness, "fresh")
        self.assertEqual(late.freshness, "stale")

    def test_cn_lunch_and_post_close_are_not_active_stale_sessions(self):
        holding = _holding("AAA", "exchange")
        quote = Quote("AAA", "AAA", 10, 1, "2026-09-11T00:00:00+00:00")
        for local_hour, local_minute, expected in (
            (11, 45, "fresh"),
            (13, 15, "stale"),
            (15, 1, "fresh"),
        ):
            now = datetime(2026, 9, 11, local_hour - 8, local_minute, tzinfo=timezone.utc)
            with self.subTest(local_hour=local_hour, local_minute=local_minute):
                router = PortfolioValuationRouter(
                    _ExchangeCollector({"AAA": quote}), _Provider(), _Provider(),
                    clock=lambda now=now: now,
                )
                self.assertEqual(router.value_holding(holding).freshness, expected)

    def test_hstech_uses_hk_sessions_for_stale_checks(self):
        quote = Quote("HK.HSTECH", "HSTECH", 3788, 0, "2026-09-11T00:00:00+00:00", "hk")
        holding = _holding("HSTECH", "qdii_nav")
        for local_hour, local_minute, expected in (
            (12, 5, "fresh"),
            (13, 15, "stale"),
            (16, 11, "fresh"),
        ):
            now = datetime(2026, 9, 11, local_hour - 8, local_minute, tzinfo=timezone.utc)
            with self.subTest(local_hour=local_hour, local_minute=local_minute):
                hk = _Provider({"HK.HSTECH": quote})
                router = PortfolioValuationRouter(
                    _ExchangeCollector(), _Provider(), hk, clock=lambda now=now: now
                )
                self.assertEqual(router.value_holding(holding).freshness, expected)

    def test_rejects_quote_for_different_code(self):
        exchange = _ExchangeCollector({
            "AAA": Quote("BBB", "BBB", 10, 1, "2026-09-11T01:30:00+00:00"),
        })
        router = PortfolioValuationRouter(exchange, _Provider(), _Provider(), clock=lambda: self.NOW)

        with self.assertRaises(ValueError):
            router.value_holding(_holding("AAA", "exchange"))

    def test_rejects_materially_future_source_timestamp(self):
        exchange = _ExchangeCollector({
            "AAA": Quote("AAA", "AAA", 10, 1, "2026-09-11T04:02:00+00:00"),
        })
        router = PortfolioValuationRouter(exchange, _Provider(), _Provider(), clock=lambda: self.NOW)

        with self.assertRaises(ValueError):
            router.value_holding(_holding("AAA", "exchange"))

    def test_mainland_holidays_roll_back_recent_nav_dates(self):
        for now, source_date, expected in (
            (datetime(2026, 2, 23, 4, tzinfo=timezone.utc), "2026-02-13", "fresh"),
            (datetime(2026, 2, 24, 4, tzinfo=timezone.utc), "2026-02-13", "fresh"),
            (datetime(2026, 2, 24, 4, tzinfo=timezone.utc), "2026-02-23", "stale"),
        ):
            with self.subTest(now=now, source_date=source_date):
                nav = _Provider({
                    "016496": Quote("016496", "016496", 1.1, 0, source_date, "nav", source_date),
                })
                router = PortfolioValuationRouter(
                    _ExchangeCollector(), nav, _Provider(), clock=lambda: now
                )
                self.assertEqual(
                    router.value_holding(_holding("016496", "fund_nav")).freshness,
                    expected,
                )

    def test_marks_old_qdii_nav_lagged_and_preserves_source_date(self):
        nav = _Provider({
            "016280": Quote("016280", "016280", 2.5, 0.1, "2026-09-08", "nav", "2026-09-08"),
        })
        router = PortfolioValuationRouter(_ExchangeCollector(), nav, _Provider(), clock=lambda: self.NOW)

        valuation = router.value_holding(_holding("016280", "qdii_nav"))

        self.assertEqual(valuation.as_of.date().isoformat(), "2026-09-08")
        self.assertEqual(valuation.freshness, "lagged")
        self.assertNotEqual(valuation.freshness, "stale")

    def test_weekend_rolls_nav_expectations_back_to_weekdays(self):
        for weekend_now in (
            datetime(2026, 9, 12, 4, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc),
        ):
            with self.subTest(weekend_now=weekend_now):
                weekend_nav = _Provider({
                    "016496": Quote(
                        "016496", "016496", 1.1, 0.0, "2026-09-12", "nav", "2026-09-12"
                    ),
                })
                router = PortfolioValuationRouter(
                    _ExchangeCollector(), weekend_nav, _Provider(), clock=lambda: weekend_now
                )
                valuation = router.value_holding(_holding("016496", "fund_nav"))
                self.assertEqual(valuation.freshness, "stale")

                weekday_nav = _Provider({
                    "016496": Quote(
                        "016496", "016496", 1.1, 0.0, "2026-09-11", "nav", "2026-09-11"
                    ),
                })
                weekday_router = PortfolioValuationRouter(
                    _ExchangeCollector(), weekday_nav, _Provider(), clock=lambda: weekend_now
                )
                self.assertEqual(
                    weekday_router.value_holding(_holding("016496", "fund_nav")).freshness,
                    "fresh",
                )

    def test_weekend_qdii_nav_is_lagged_not_fresh(self):
        weekend_now = datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc)
        nav = _Provider({
            "016280": Quote(
                "016280", "016280", 2.5, 0.1, "2026-09-13", "nav", "2026-09-13"
            ),
        })
        router = PortfolioValuationRouter(
            _ExchangeCollector(), nav, _Provider(), clock=lambda: weekend_now
        )

        valuation = router.value_holding(_holding("016280", "qdii_nav"))

        self.assertEqual(valuation.freshness, "lagged")


if __name__ == "__main__":
    unittest.main()
