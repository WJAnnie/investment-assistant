"""Offline construction tests for the overnight global-market providers.

The factory is a composition-root seam: it must build providers eagerly enough
that a wiring mistake is visible at startup, but never perform a request and
never relax the transport isolation the adapter already enforces.
"""

import unittest
from unittest.mock import patch

import requests

from app.market.factory import create_global_market_providers
from app.market.global_markets import FredTreasuryProvider, YahooGlobalMarketProvider


class GlobalMarketProviderFactoryTests(unittest.TestCase):
    def test_factory_returns_instrument_and_treasury_providers(self):
        global_provider, treasury_fallback = create_global_market_providers()

        self.assertIsInstance(global_provider, YahooGlobalMarketProvider)
        self.assertIsInstance(treasury_fallback, FredTreasuryProvider)

    def test_default_providers_ignore_environment_credentials(self):
        global_provider, treasury_fallback = create_global_market_providers()

        # A proxied or .netrc-backed session would silently borrow ambient
        # credentials, so both transports must have already refused them.
        self.assertIs(global_provider.session.trust_env, False)
        self.assertIs(treasury_fallback.session.trust_env, False)

    def test_environment_credentials_do_not_leak_into_a_default_session(self):
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "NETRC"):
            with self.subTest(variable=variable), patch.dict(
                "os.environ", {variable: "http://synthetic-proxy.invalid"}, clear=False
            ):
                global_provider, treasury_fallback = create_global_market_providers()

                self.assertIs(global_provider.session.trust_env, False)
                self.assertIs(treasury_fallback.session.trust_env, False)

    def test_injected_session_is_reused_instead_of_replaced(self):
        session = requests.Session()
        try:
            global_provider, treasury_fallback = create_global_market_providers(session=session)

            self.assertIs(global_provider.session, session)
            self.assertIs(treasury_fallback.session, session)
        finally:
            session.close()

    def test_injected_session_is_still_hardened_against_redirects(self):
        session = requests.Session()
        with patch.object(requests.Session, "request", side_effect=AssertionError("no request")):
            try:
                global_provider, _ = create_global_market_providers(session=session)
            finally:
                session.close()

        self.assertIs(global_provider.session.trust_env, False)

    def test_construction_performs_no_network_request(self):
        with patch.object(
            requests.Session, "get", side_effect=AssertionError("factory must not fetch")
        ) as get, patch.object(
            requests.Session, "request", side_effect=AssertionError("factory must not fetch")
        ) as request:
            global_provider, treasury_fallback = create_global_market_providers()

        get.assert_not_called()
        request.assert_not_called()
        self.assertIsNotNone(global_provider)
        self.assertIsNotNone(treasury_fallback)

    def test_the_two_providers_use_distinct_source_labels(self):
        global_provider, treasury_fallback = create_global_market_providers()

        # Sharing one label would make a Treasury fallback indistinguishable
        # from a primary observation in the rendered report.
        self.assertEqual(global_provider.SOURCE, "yahoo_chart")
        self.assertEqual(treasury_fallback.SOURCE, "fred_dgs10")


if __name__ == "__main__":
    unittest.main()
