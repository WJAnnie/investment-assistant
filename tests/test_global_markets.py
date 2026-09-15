"""Offline parser tests for the keyless public global-market adapter.

Every response used here is a local fixture built in this file. No test opens a
socket, reads configuration, credentials or account data, and every hostile
case asserts that no remote text, URL or header can leak into an error.
"""

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from app.market import global_markets as gm


ROOT_SYMBOL = "^GSPC"
# Five weekday sessions, oldest first; the last one is the fixture's "today".
SESSIONS = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10),
            date(2026, 9, 11), date(2026, 9, 14))
OPEN_UTC = (13, 30)   # 09:30 in America/New_York while DST is in effect
CLOSE_UTC = (20, 0)   # 16:00 in America/New_York while DST is in effect
UNSET = object()      # sentinel: use the fixture's normal value
MISSING = object()    # sentinel: serialize the payload with a key removed
FRAGMENT = "__CLOSES_FRAGMENT__"
SECRET_REMOTE_TEXT = "REMOTE-TEXT-SENTINEL-4f21"
SECRET_HEADER_VALUE = "BEARER-TOKEN-SENTINEL-8c3d"


def epoch(day, clock=OPEN_UTC):
    return int(datetime(day.year, day.month, day.day, clock[0], clock[1],
                        tzinfo=timezone.utc).timestamp())


def _optional(meta, key, value):
    """UNSET was resolved by the caller; MISSING omits the key, None sends null."""
    if value is MISSING:
        meta.pop(key, None)
    else:
        meta[key] = value


def base_payload(symbol=ROOT_SYMBOL, closes=(100.0, None, 102.0), *, timestamps=None,
                 instrument_type=UNSET, market_time=UNSET, meta_symbol=UNSET,
                 chart_error=None):
    """Build a Yahoo-shaped chart payload as a Python object.

    ``UNSET`` means "use the fixture's normal value", ``MISSING`` means "omit the
    key entirely", and ``None`` reaches the parser as a real JSON ``null``.
    """
    if timestamps is None:
        days = SESSIONS[-len(closes):] if closes else ()
        timestamps = [epoch(day) for day in days]
    meta = {"currency": "USD", "exchangeName": "SNP"}
    _optional(meta, "symbol", symbol if meta_symbol is UNSET else meta_symbol)
    _optional(meta, "instrumentType",
              gm.INSTRUMENTS[symbol][4] if instrument_type is UNSET else instrument_type)
    _optional(meta, "regularMarketTime",
              epoch(SESSIONS[-1], CLOSE_UTC) if market_time is UNSET else market_time)
    return {
        "chart": {
            "result": [{
                "meta": meta,
                "timestamp": list(timestamps),
                "indicators": {"quote": [{"close": list(closes)}]},
            }],
            "error": chart_error,
        }
    }


def encode(payload):
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


def yahoo_payload(symbol=ROOT_SYMBOL, closes=(100.0, None, 102.0), **overrides):
    """Serialize a normal fixture; ``allow_nan=False`` keeps hostile floats out."""
    return encode(base_payload(symbol, closes, **overrides))


def yahoo_raw_payload(symbol=ROOT_SYMBOL, closes_json="[100.0, null, 102.0]", **overrides):
    """Inject a raw JSON fragment so hostile literals reach the parser verbatim."""
    literal = json.loads(closes_json)
    if not isinstance(literal, list) or not literal:
        raise AssertionError(f"closes_json must be a non-empty array: {closes_json!r}")
    placeholder = [FRAGMENT] * len(literal)
    overrides.setdefault("timestamps", [epoch(day) for day in SESSIONS[-len(literal):]])
    text = yahoo_payload(symbol, tuple(placeholder), **overrides).decode("utf-8")
    needle = json.dumps(placeholder)
    if needle not in text:
        raise AssertionError(f"fixture lost its close placeholder: {text!r}")
    return text.replace(needle, closes_json).encode("utf-8")


class FakeResponse:
    """Chunked response stand-in; nothing here touches a socket."""

    def __init__(self, body, status_code=200):
        if isinstance(body, str):
            raise TypeError("fixtures must be bytes")
        self._body = bytes(body)
        self.status_code = status_code
        self.closed = False

    @property
    def content(self):
        return self._body

    def iter_content(self, chunk_size=None):
        step = chunk_size or 4096
        for start in range(0, len(self._body), step):
            yield self._body[start:start + step]

    def close(self):
        self.closed = True


class FakeSession:
    """Record every call locally; never performs I/O."""

    def __init__(self, body=b"", status_code=200, error=None):
        self.body = body
        self.status_code = status_code
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body, self.status_code)


class GlobalMarketProviderTests(unittest.TestCase):
    def setUp(self):
        self.session = None

    def provider(self, body=b"", *, status_code=200, error=None):
        self.session = FakeSession(body, status_code=status_code, error=error)
        return gm.YahooGlobalMarketProvider(session=self.session)

    def observation(self, symbol, body, **kwargs):
        return self.provider(body, **kwargs).fetch(symbol)

    def assertFails(self, code, body, symbol=ROOT_SYMBOL, *, status_code=200, error=None):
        provider = self.provider(body, status_code=status_code, error=error)
        with self.assertRaises(gm.GlobalMarketDataError) as caught:
            provider.fetch(symbol)
        failure = caught.exception
        self.assertEqual(failure.code, code)
        self.assertIn(code, gm.ERROR_CODES)
        self.assertEqual(str(failure), code)
        # No chained cause means no remote exception text can surface later.
        self.assertIsNone(failure.__cause__)
        # One bounded attempt per symbol: the adapter has no retry loop.
        self.assertEqual(len(self.session.calls), 1)
        return failure

    # ---- success path -----------------------------------------------------

    def test_yahoo_returns_latest_and_previous_valid_daily_close(self):
        session = FakeSession(yahoo_payload("^GSPC", [100.0, None, 102.0]))
        quote = gm.YahooGlobalMarketProvider(session=session).fetch("^GSPC")
        self.assertEqual(quote.symbol, "^GSPC")
        self.assertEqual(quote.value, 102.0)
        self.assertEqual(quote.previous_value, 100.0)
        self.assertEqual(quote.session_date, "2026-09-14")
        self.assertEqual(quote.source_as_of, "2026-09-14T20:00:00+00:00")
        self.assertEqual(session.calls[0]["timeout"], 10)

    def test_request_uses_the_fixed_host_encoded_symbol_and_bounded_options(self):
        quote = self.observation("^GSPC", yahoo_payload("^GSPC"))
        (call,) = self.session.calls
        # The URL stops at the encoded symbol; the query is sent as params.
        self.assertEqual(
            call["url"],
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC",
        )
        self.assertNotIn("?", call["url"])
        self.assertEqual(call["params"], {"interval": "1d", "range": "5d"})
        self.assertEqual(call["timeout"], 10)
        self.assertIs(call["allow_redirects"], False)
        self.assertEqual(quote.source, "yahoo_chart")
        for name, value in call["headers"].items():
            self.assertNotIn(SECRET_HEADER_VALUE, f"{name}:{value}")
            self.assertNotIn(name.lower(), {"authorization", "cookie", "proxy-authorization"})

    def test_keeps_multi_character_symbols_intact_and_type_strict(self):
        session = FakeSession(yahoo_payload("DX-Y.NYB", [100.0, 101.0]))
        quote = gm.YahooGlobalMarketProvider(session=session).fetch("DX-Y.NYB")
        self.assertEqual(
            session.calls[0]["url"],
            "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB",
        )
        self.assertEqual(session.calls[0]["params"], {"interval": "1d", "range": "5d"})
        self.assertAlmostEqual(quote.value, 101.0)
        self.assertAlmostEqual(quote.previous_value, 100.0)

        futures = self.observation("GC=F", yahoo_payload("GC=F", [2500.0, 2510.5]))
        self.assertAlmostEqual(futures.value, 2510.5)

    def test_session_date_uses_the_catalog_market_timezone(self):
        # 2026-09-15T04:30Z is 00:30 in New York but still 23:30 on 09-14 in Chicago.
        stamps = [int(datetime(2026, 9, 11, 4, 30, tzinfo=timezone.utc).timestamp()),
                  int(datetime(2026, 9, 15, 4, 30, tzinfo=timezone.utc).timestamp())]
        new_york = self.observation(
            "^GSPC", yahoo_payload("^GSPC", [1.0, 2.0], timestamps=stamps,
                                   market_time=stamps[-1]))
        chicago = self.observation(
            "^TNX", yahoo_payload("^TNX", [4.5, 4.6], timestamps=stamps,
                                  market_time=stamps[-1]))
        self.assertEqual(new_york.session_date, "2026-09-15")
        self.assertEqual(chicago.session_date, "2026-09-14")
        self.assertEqual(new_york.source_as_of, "2026-09-15T04:30:00+00:00")

    def test_catalog_freezes_the_eight_approved_instruments(self):
        self.assertEqual(list(gm.INSTRUMENTS), [
            "^GSPC", "^DJI", "^IXIC", "^SOX", "^TNX", "DX-Y.NYB", "GC=F", "CL=F",
        ])
        self.assertEqual(gm.INSTRUMENTS["^GSPC"],
                         ("us_equities", "标普500", "points", "America/New_York", "INDEX"))
        self.assertEqual(gm.INSTRUMENTS["^DJI"],
                         ("us_equities", "道琼斯工业指数", "points", "America/New_York", "INDEX"))
        self.assertEqual(gm.INSTRUMENTS["^IXIC"],
                         ("nasdaq", "纳斯达克综合指数", "points", "America/New_York", "INDEX"))
        self.assertEqual(gm.INSTRUMENTS["^SOX"],
                         ("semiconductors", "费城半导体指数", "points", "America/New_York", "INDEX"))
        self.assertEqual(gm.INSTRUMENTS["^TNX"],
                         ("us_treasury", "美国10年期国债收益率", "percent", "America/Chicago", "INDEX"))
        self.assertEqual(gm.INSTRUMENTS["DX-Y.NYB"],
                         ("usd", "美元指数", "points", "America/New_York", "INDEX"))
        self.assertEqual(gm.INSTRUMENTS["GC=F"],
                         ("gold", "COMEX黄金", "usd_per_ounce", "America/New_York", "FUTURE"))
        self.assertEqual(gm.INSTRUMENTS["CL=F"],
                         ("oil", "WTI原油", "usd_per_barrel", "America/New_York", "FUTURE"))

    def test_observation_is_immutable_and_carries_no_trade_action(self):
        quote = self.observation("^GSPC", yahoo_payload())
        with self.assertRaises(FrozenInstanceError):
            quote.value = 1.0
        for field in ("action", "signal", "trade", "score"):
            self.assertFalse(hasattr(quote, field))

    # ---- untrusted symbol and transport boundaries ------------------------

    def test_rejects_unsupported_symbols_before_any_request(self):
        provider = self.provider(yahoo_payload())
        for symbol in ("^NDX", "AAPL", "GSPC", "", None, 5, ["^GSPC"], "^gspc"):
            with self.subTest(symbol=symbol), self.assertRaises(gm.GlobalMarketDataError) as caught:
                provider.fetch(symbol)
            self.assertEqual(caught.exception.code, gm.ERROR_UNSUPPORTED_SYMBOL)
        self.assertEqual(self.session.calls, [])

    def test_transport_failures_become_a_fixed_error_code(self):
        for error in (requests.Timeout("SECRET"), requests.ConnectionError("SECRET"),
                      requests.RequestException("SECRET"), RuntimeError(SECRET_REMOTE_TEXT)):
            with self.subTest(error=type(error).__name__):
                failure = self.assertFails(gm.ERROR_NETWORK, yahoo_payload(), error=error)
                self.assertNotIn(SECRET_REMOTE_TEXT, repr(failure))

    def test_rejects_redirects_rate_limits_and_other_statuses(self):
        for status_code, code in ((301, gm.ERROR_REDIRECT), (302, gm.ERROR_REDIRECT),
                                  (307, gm.ERROR_REDIRECT), (429, gm.ERROR_RATE_LIMITED),
                                  (400, gm.ERROR_HTTP_STATUS), (404, gm.ERROR_HTTP_STATUS),
                                  (500, gm.ERROR_HTTP_STATUS), (503, gm.ERROR_HTTP_STATUS)):
            with self.subTest(status_code=status_code):
                self.assertFails(code, yahoo_payload(), status_code=status_code)

    def test_rejects_oversized_bodies_while_streaming(self):
        payload = yahoo_payload()
        body = payload + b" " * (gm.MAX_BODY_BYTES + 1 - len(payload))
        self.assertEqual(len(body), gm.MAX_BODY_BYTES + 1)
        self.assertFails(gm.ERROR_TOO_LARGE, body)

    def test_default_session_disables_proxy_credentials_and_redirects(self):
        provider = gm.YahooGlobalMarketProvider()
        self.assertIs(provider.session.trust_env, False)
        response = SimpleNamespace(status_code=302)
        with patch.object(requests.Session, "request", return_value=response) as request:
            with self.assertRaises(gm.GlobalMarketDataError) as caught:
                provider.fetch("^GSPC")
        self.assertEqual(caught.exception.code, gm.ERROR_REDIRECT)
        self.assertIs(request.call_args.kwargs["allow_redirects"], False)

    # ---- hostile payload shaping -----------------------------------------

    def test_rejects_a_symbol_the_provider_did_not_ask_for(self):
        self.assertFails(gm.ERROR_SYMBOL_MISMATCH, yahoo_payload(meta_symbol="^GSPCX"))
        self.assertFails(gm.ERROR_SYMBOL_MISMATCH, yahoo_payload(meta_symbol="^DJI"))
        self.assertFails(gm.ERROR_SYMBOL_MISMATCH, yahoo_payload(meta_symbol=None))
        self.assertFails(gm.ERROR_SYMBOL_MISMATCH, encode(base_payload(meta_symbol=MISSING)))

    def test_rejects_a_substituted_instrument_type(self):
        for instrument_type in ("EQUITY", "ETF", "FUTURE", "index", ""):
            with self.subTest(instrument_type=instrument_type):
                self.assertFails(gm.ERROR_TYPE_MISMATCH,
                                 yahoo_payload(instrument_type=instrument_type))
        self.assertFails(gm.ERROR_TYPE_MISMATCH,
                         yahoo_payload("GC=F", instrument_type="INDEX"), symbol="GC=F")

    def test_rejects_malformed_json_and_unknown_shapes(self):
        for body in (b"", b"{", b"[]", b"null", b'"text"', b"\xff\xfe\x00\x01",
                     b'{"chart": null}', b'{"chart": []}',
                     b'{"chart": {"error": null}}',
                     b'{"chart": {"result": [], "error": null}}',
                     b'{"chart": {"result": null, "error": null}}',
                     b'{"chart": {"result": [null], "error": null}}'):
            with self.subTest(body=body[:20]):
                self.assertFails(gm.ERROR_MALFORMED, body)

    def test_rejects_non_standard_json_constants(self):
        self.assertFails(gm.ERROR_MALFORMED, yahoo_raw_payload(closes_json="[100.0, null, NaN]"))
        self.assertFails(gm.ERROR_MALFORMED, yahoo_raw_payload(closes_json="[100.0, null, Infinity]"))

    def test_rejects_source_reported_errors(self):
        self.assertFails(gm.ERROR_SOURCE_ERROR,
                         yahoo_payload(chart_error={"code": "Not Found",
                                                    "description": SECRET_REMOTE_TEXT}))

    def test_rejects_missing_or_broken_series_shapes(self):
        shapes = (
            lambda payload: payload["chart"]["result"][0].pop("indicators"),
            lambda payload: payload["chart"]["result"][0].pop("timestamp"),
            lambda payload: payload["chart"]["result"][0].pop("meta"),
            lambda payload: payload["chart"]["result"][0]["indicators"].pop("quote"),
            lambda payload: payload["chart"]["result"][0]["indicators"].update(quote={}),
            lambda payload: payload["chart"]["result"][0]["indicators"].update(quote=[]),
            lambda payload: payload["chart"]["result"][0].update(
                indicators={"quote": [{"close": None}]}),
            lambda payload: payload["chart"]["result"][0].update(
                timestamp="2026-09-14"),
            lambda payload: payload["chart"]["result"][0].update(
                timestamp=[epoch(SESSIONS[0]), epoch(SESSIONS[1])]),
            lambda payload: payload["chart"]["result"][0].update(meta=[]),
            lambda payload: payload["chart"]["result"].append({}),
            lambda payload: payload["chart"]["result"].clear(),
        )
        for shape in shapes:
            with self.subTest(shape=shape.__code__.co_firstlineno):
                payload = base_payload()
                shape(payload)
                self.assertFails(gm.ERROR_MALFORMED, encode(payload))

    def test_rejects_fewer_than_two_valid_closes(self):
        for closes in ([], [100.0], [100.0, None, None], [None, None, None]):
            with self.subTest(closes=closes):
                self.assertFails(gm.ERROR_INSUFFICIENT, yahoo_payload(closes=closes))

    def test_rejects_invalid_close_values(self):
        for closes_json in ("[100.0, null, 1e1000]", "[100.0, null, 0.0]",
                            "[100.0, null, -1.0]", '[100.0, null, "102.0"]',
                            "[100.0, null, true]", "[100.0, null, [102.0]]",
                            "[100.0, null, 1" + "0" * 400 + "]"):
            with self.subTest(closes=closes_json[:24]):
                self.assertFails(gm.ERROR_INVALID_VALUE, yahoo_raw_payload(closes_json=closes_json))
        # The previous value is validated the same way.
        self.assertFails(gm.ERROR_INVALID_VALUE, yahoo_raw_payload(closes_json="[0.0, 102.0]"))

    def test_rejects_missing_or_unconvertible_source_time(self):
        for market_time in (MISSING, None, 0, -1, "2026-09-14T20:00:00Z", 1789000000.5,
                            10 ** 30, True):
            with self.subTest(market_time=market_time):
                self.assertFails(gm.ERROR_INVALID_TIME,
                                 encode(base_payload(market_time=market_time)))

    def test_rejects_unconvertible_or_unsorted_daily_timestamps(self):
        # Element-level time problems share one code whether they are out of
        # range, negative, duplicated, misordered or not even numbers.
        for timestamps in ([epoch(SESSIONS[0]), 10 ** 30],
                           [epoch(SESSIONS[1]), epoch(SESSIONS[0])],
                           [epoch(SESSIONS[0]), epoch(SESSIONS[0])],
                           [-1, epoch(SESSIONS[0])],
                           [epoch(SESSIONS[0]), True],
                           [epoch(SESSIONS[0]), None],
                           [epoch(SESSIONS[0]), "2026-09-14"]):
            with self.subTest(timestamps=timestamps):
                self.assertFails(gm.ERROR_INVALID_TIME,
                                 encode(base_payload(closes=[1.0, 2.0], timestamps=timestamps)))
        # A non-array timestamp field is structural, not a value problem.
        self.assertFails(gm.ERROR_MALFORMED,
                         encode(base_payload(closes=[1.0, 2.0],
                                             timestamps={"0": epoch(SESSIONS[0])})))

    def test_failures_never_embed_remote_text_url_or_headers(self):
        failure = self.assertFails(
            gm.ERROR_SOURCE_ERROR,
            encode(base_payload(chart_error={"code": "Not Found",
                                             "description": SECRET_REMOTE_TEXT})))
        self.assertNotIn(SECRET_REMOTE_TEXT, repr(failure))
        raw = b'{"chart": "' + SECRET_REMOTE_TEXT.encode("utf-8") + b'"'
        failure = self.assertFails(gm.ERROR_MALFORMED, raw)
        for value in (SECRET_REMOTE_TEXT, "query1.finance.yahoo.com", "https://", "?interval"):
            self.assertNotIn(value, repr(failure))


if __name__ == "__main__":
    unittest.main()
