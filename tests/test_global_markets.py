"""Offline parser tests for the keyless public global-market adapter.

Every response used here is a local fixture built in this file. No test opens a
socket, reads configuration, credentials or account data, and every hostile
case asserts that no remote text, URL or header can leak into an error.
"""

from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
import json
from types import MappingProxyType, SimpleNamespace
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


def _module_bindings():
    """Yield ``(name, value)`` for the module state the adapter itself defines.

    ``vars(gm)`` is not the adapter's private state: Python injects a live
    ``__builtins__`` dict (and other bookkeeping) into every module namespace, so
    a blind "no dict" sweep would flag the interpreter rather than a leak. Only
    bindings the module could have used to shadow its own catalog are yielded;
    the interpreter-injected ones are left out on purpose.
    """
    namespace = vars(gm)
    for name in sorted(namespace):
        if name.startswith("__") and name.endswith("__"):
            continue
        value = namespace[name]
        if isinstance(value, Mapping):
            yield name, value


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
              gm.INSTRUMENTS[symbol].instrument_type
              if instrument_type is UNSET else instrument_type)
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


RAW_TOKEN = "__RAW_JSON_TOKEN__"


def raw_token_payload(payload, token):
    """Serialize ``payload``, splicing ``token`` in verbatim at ``RAW_TOKEN``.

    Hostile number literals must reach the parser exactly as written, which
    ``json.dumps(..., allow_nan=False)`` cannot produce.
    """
    text = encode(payload).decode("utf-8")
    needle = json.dumps(RAW_TOKEN)
    if needle not in text:
        raise AssertionError(f"fixture lost its raw token placeholder: {text!r}")
    return text.replace(needle, token).encode("utf-8")


def payload_node_count(payload):
    """Count containers, object keys and scalar values without recursing."""
    nodes = 0
    pending = [payload]
    while pending:
        value = pending.pop()
        nodes += 1
        if type(value) is dict:
            for _key, item in value.items():
                nodes += 1
                pending.append(item)
        elif type(value) is list:
            pending.extend(value)
    return nodes


def node_budget_payload(node_budget):
    """Fill ``base_payload()`` with inert padding up to exactly ``node_budget``.

    The filler lives in ``padding_*`` arrays of at most
    ``MAX_CONTAINER_WIDTH`` entries, so only the total node count is stressed
    and never the per-container width cap.
    """
    payload = base_payload()
    base_nodes = payload_node_count(payload)
    containers = 1
    while True:
        scalars = node_budget - base_nodes - 2 * containers
        if scalars >= 0 and -(-scalars // gm.MAX_CONTAINER_WIDTH) <= containers:
            break
        containers += 1
    remaining = scalars
    for index in range(containers):
        size = min(gm.MAX_CONTAINER_WIDTH, remaining)
        payload[f"padding_{index}"] = [1] * size
        remaining -= size
    if remaining or payload_node_count(payload) != node_budget:
        raise AssertionError("fixture failed to hit the requested node budget")
    return payload


def series_timestamps(count):
    """``count`` ascending daily epochs starting inside the accepted window."""
    return [gm.MIN_SOURCE_EPOCH + 86400 * index for index in range(count)]


def white_padded(body, size=gm.MAX_BODY_BYTES):
    """Right-pad a legal body with JSON-insignificant whitespace to ``size``."""
    if len(body) > size:
        raise AssertionError(f"body of {len(body)} bytes exceeds the padding cap")
    return body + b" " * (size - len(body))


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
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body, self.status_code)


class StaticResponseSession:
    """Return one caller-supplied response without touching the network."""

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


class BoundaryResponse:
    """Expose failures at each untrusted response protocol boundary."""

    def __init__(self, body=None, *, phase=None, chunks=None, status=200,
                 close_phase=None):
        self._body = yahoo_payload() if body is None else body
        self._phase = phase
        self._chunks = chunks
        self._status = status
        self._close_phase = close_phase

    @property
    def status_code(self):
        if self._phase == "status_property":
            raise RuntimeError(SECRET_REMOTE_TEXT)
        if self._phase == "status_memory":
            raise MemoryError(SECRET_REMOTE_TEXT)
        return self._status

    @property
    def iter_content(self):
        if self._phase == "iter_property":
            raise RuntimeError(SECRET_REMOTE_TEXT)
        if self._phase == "iter_memory":
            raise MemoryError(SECRET_REMOTE_TEXT)

        def open_stream(_chunk_size):
            if self._phase == "iter_call":
                raise RuntimeError(SECRET_REMOTE_TEXT)
            if self._phase == "iter_iteration":
                def broken_stream():
                    yield self._body[:1]
                    raise RuntimeError(SECRET_REMOTE_TEXT)
                return broken_stream()
            if self._phase == "iter_iteration_memory":
                def exhausted_stream():
                    yield self._body[:1]
                    raise MemoryError(SECRET_REMOTE_TEXT)
                return exhausted_stream()
            return iter(self._chunks if self._chunks is not None else (self._body,))

        return open_stream

    @property
    def close(self):
        if self._close_phase == "property":
            raise RuntimeError(SECRET_REMOTE_TEXT)

        def close_response():
            if self._close_phase == "call":
                raise RuntimeError(SECRET_REMOTE_TEXT)

        return close_response


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
        # Neither cause nor context may retain remote text for later inspection.
        self.assertIsNone(failure.__cause__)
        self.assertIsNone(failure.__context__)
        # One bounded attempt per symbol: the adapter has no retry loop.
        self.assertEqual(len(self.session.calls), 1)
        return failure

    def assertResponseFails(self, code, response):
        session = StaticResponseSession(response)
        provider = gm.YahooGlobalMarketProvider(session=session)
        with self.assertRaises(gm.GlobalMarketDataError) as caught:
            provider.fetch(ROOT_SYMBOL)
        failure = caught.exception
        self.assertEqual(failure.code, code)
        self.assertEqual(str(failure), code)
        self.assertIsNone(failure.__cause__)
        self.assertIsNone(failure.__context__)
        self.assertNotIn(SECRET_REMOTE_TEXT, repr(failure))
        self.assertEqual(len(session.calls), 1)
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

    def test_observation_has_exactly_six_dataclass_fields(self):
        self.assertEqual(tuple(gm.SourceObservation.__dataclass_fields__), (
            "symbol", "value", "previous_value", "source_as_of", "session_date", "source",
        ))

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

    def test_timeout_is_a_finite_positive_number_capped_at_ten_seconds(self):
        messages = set()
        for invalid in (None, False, True, 0, 0.0, -1, "1", float("nan"),
                        float("inf"), float("-inf"), 10.0001):
            with self.subTest(invalid=invalid):
                session = FakeSession(yahoo_payload())
                with self.assertRaises(ValueError) as caught:
                    gm.YahooGlobalMarketProvider(session=session, timeout=invalid)
                messages.add(str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
                self.assertEqual(session.calls, [])
        self.assertEqual(len(messages), 1)
        self.assertNotIn("nan", next(iter(messages)).lower())

        session = FakeSession(yahoo_payload())
        gm.YahooGlobalMarketProvider(session=session, timeout=0.25).fetch(ROOT_SYMBOL)
        self.assertEqual(session.calls[0]["timeout"], 0.25)

    def test_equals_in_symbol_is_percent_encoded(self):
        self.observation("GC=F", yahoo_payload("GC=F", [2500.0, 2510.5]))
        self.assertEqual(
            self.session.calls[0]["url"],
            "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF",
        )

    def test_rejects_success_statuses_without_a_valid_payload(self):
        for status_code in (201, 204):
            with self.subTest(status_code=status_code):
                self.assertFails(gm.ERROR_HTTP_STATUS, yahoo_payload(), status_code=status_code)

    def test_accepts_exactly_one_megabyte_body(self):
        payload = yahoo_payload()
        body = payload + b" " * (gm.MAX_BODY_BYTES - len(payload))
        self.assertEqual(len(body), gm.MAX_BODY_BYTES)
        self.observation("^GSPC", body)

    def test_returns_the_final_two_valid_closes_from_longer_history(self):
        closes = [100.0, None, 101.0, None, 102.0, 103.0]
        timestamps = [epoch(day) for day in SESSIONS] + [epoch(SESSIONS[-1], CLOSE_UTC)]
        quote = self.observation("^GSPC", yahoo_payload("^GSPC", closes, timestamps=timestamps))
        self.assertEqual((quote.previous_value, quote.value), (102.0, 103.0))

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
        self.assertEqual(gm.INSTRUMENTS["^GSPC"].timezone, "America/New_York")
        with self.assertRaises(TypeError):
            gm.INSTRUMENTS["^GSPC"] = gm.INSTRUMENTS["^DJI"]
        with self.assertRaises(TypeError):
            gm.INSTRUMENTS["^NDX"] = gm.INSTRUMENTS["^GSPC"]
        with self.assertRaises(AttributeError):
            gm.INSTRUMENTS["^GSPC"].timezone = "UTC"

    def test_catalog_base_map_is_unreachable_and_rows_stay_frozen(self):
        symbols = set(gm.INSTRUMENTS)
        # The mutable map behind the proxy must not have a name at all: a
        # module-level ``_INSTRUMENTS`` would let any importer swap a row while
        # the provider keeps serving its frozen-looking view.
        self.assertFalse(hasattr(gm, "_INSTRUMENTS"))
        self.assertIs(type(gm.INSTRUMENTS), MappingProxyType)

        # Sweep everything this module could own for a mapping *keyed by the
        # catalog symbols*: only the read-only proxy may carry that key set, so
        # neither the interpreter's ``__builtins__`` dict nor the adapter's own
        # ``_REQUEST_HEADERS`` dict is mistaken for a base map.
        catalog_keyed = [(name, value) for name, value in _module_bindings()
                         if symbols & set(value)]
        self.assertEqual([name for name, _value in catalog_keyed], ["INSTRUMENTS"])
        (base_candidate_name, base_candidate), = catalog_keyed
        self.assertEqual(base_candidate_name, "INSTRUMENTS")
        self.assertIs(base_candidate, gm.INSTRUMENTS)
        self.assertIs(gm.catalog_entry("^GSPC"), gm.INSTRUMENTS["^GSPC"])

        # Positive controls: the namespace really does hold a live mutable
        # ``dict`` that a blind sweep would have flagged, and the sweep is
        # selective because it looks for catalog keys rather than any ``dict``.
        self.assertIn("__builtins__", vars(gm))
        self.assertNotIn("__builtins__", [name for name, _value in _module_bindings()])
        self.assertTrue(any(type(value) is dict for value in vars(gm).values()))
        builtins_binding = vars(gm)["__builtins__"]
        if type(builtins_binding) is dict:
            self.assertFalse(symbols & set(builtins_binding))
        # A copy is an independent mapping, so swapping a row inside it cannot
        # reach the catalog: ``gm.INSTRUMENTS["^GSPC"]`` must stay the original.
        laundered = gm.INSTRUMENTS.copy()
        laundered["^GSPC"] = gm.INSTRUMENTS["^DJI"]
        self.assertIsNot(laundered, gm.INSTRUMENTS)
        self.assertIs(laundered["^GSPC"], gm.INSTRUMENTS["^DJI"])
        self.assertIsNot(gm.INSTRUMENTS["^GSPC"], laundered["^GSPC"])
        self.assertEqual(gm.INSTRUMENTS["^GSPC"].name, "标普500")
        self.assertIs(gm.catalog_entry("^GSPC"), gm.INSTRUMENTS["^GSPC"])

        row = gm.INSTRUMENTS["^GSPC"]
        self.assertIs(type(row), gm.CatalogRow)
        self.assertFalse(hasattr(row, "__dict__"))
        for attribute in ("category", "name", "value_unit", "timezone", "instrument_type"):
            with self.subTest(attribute=attribute):
                with self.assertRaises((AttributeError, TypeError)):
                    setattr(row, attribute, "hijacked")
                with self.assertRaises((AttributeError, TypeError)):
                    object.__setattr__(row, attribute, "hijacked")
                self.assertNotEqual(getattr(row, attribute), "hijacked")
        self.assertEqual(gm.catalog_entry("^GSPC"), row)
        # Rows reachable through the catalog are the very rows the proxy holds.
        self.assertEqual({id(entry) for entry in gm.INSTRUMENTS.values()},
                         {id(gm.catalog_entry(symbol)) for symbol in gm.INSTRUMENTS})
        # A copy cannot be laundered back into the catalog either; a read-only
        # proxy refuses the write whether by hiding ``update`` or by rejecting
        # the call, so either failure is accepted.
        with self.assertRaises((AttributeError, TypeError)):
            gm.INSTRUMENTS.update({"^GSPC": gm.INSTRUMENTS["^DJI"]})
        with self.assertRaises((AttributeError, TypeError)):
            gm.INSTRUMENTS.update(laundered)
        with self.assertRaises(TypeError):
            gm.INSTRUMENTS["^GSPC"] = laundered["^DJI"]

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

    def test_response_protocol_access_failures_are_fixed_and_context_free(self):
        for phase in ("status_property", "iter_property", "iter_call", "iter_iteration"):
            with self.subTest(phase=phase):
                self.assertResponseFails(gm.ERROR_NETWORK,
                                         BoundaryResponse(phase=phase))

    def test_response_stream_rejects_every_non_byte_chunk_type(self):
        for chunk in (None, "bytes", memoryview(b"bytes"), 1, 1.0, True, object()):
            with self.subTest(chunk_type=type(chunk).__name__):
                self.assertResponseFails(
                    gm.ERROR_MALFORMED,
                    BoundaryResponse(chunks=(chunk,)),
                )

    def test_close_failures_never_replace_a_success_or_primary_failure(self):
        for close_phase in ("property", "call"):
            with self.subTest(close_phase=close_phase, outcome="success"):
                session = StaticResponseSession(
                    BoundaryResponse(close_phase=close_phase))
                quote = gm.YahooGlobalMarketProvider(session=session).fetch(ROOT_SYMBOL)
                self.assertEqual(quote.value, 102.0)

            with self.subTest(close_phase=close_phase, outcome="failure"):
                self.assertResponseFails(
                    gm.ERROR_HTTP_STATUS,
                    BoundaryResponse(status=500, close_phase=close_phase),
                )

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

    def test_injected_session_is_a_trusted_test_seam_with_environment_disabled(self):
        session = FakeSession(yahoo_payload())
        provider = gm.YahooGlobalMarketProvider(session=session)
        self.assertIs(provider.session, session)
        self.assertIs(session.trust_env, False)

    def test_rejects_a_session_that_cannot_disable_environment_credentials(self):
        class LockedSession:
            @property
            def trust_env(self):
                return True

            @trust_env.setter
            def trust_env(self, _value):
                raise RuntimeError(SECRET_REMOTE_TEXT)

        with self.assertRaises(ValueError) as caught:
            gm.YahooGlobalMarketProvider(session=LockedSession())
        self.assertEqual(str(caught.exception),
                         "session must allow trust_env to be disabled")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(SECRET_REMOTE_TEXT, repr(caught.exception))

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
        self.assertFails(gm.ERROR_MALFORMED, yahoo_raw_payload(closes_json="[100.0, null, -Infinity]"))

    def test_json_nesting_is_capped_at_32_outside_strings(self):
        within_limit = 0
        for _ in range(31):
            within_limit = [within_limit]
        payload = base_payload()
        payload["padding"] = within_limit
        payload["brackets_are_text"] = "[" * 64 + "]" * 64
        self.observation(ROOT_SYMBOL, encode(payload))

        too_deep = [within_limit]
        payload["padding"] = too_deep
        self.assertFails(gm.ERROR_MALFORMED, encode(payload))

    def test_json_numeric_tokens_are_bounded_and_finite_during_parse(self):
        payload = base_payload()
        payload["numeric_padding"] = int("9" * gm.MAX_NUMERIC_TOKEN_LENGTH)
        self.observation(ROOT_SYMBOL, encode(payload))

        payload["numeric_padding"] = "NUMERIC_TOKEN"
        template = encode(payload)
        oversized = template.replace(
            b'"NUMERIC_TOKEN"',
            b"9" * (gm.MAX_NUMERIC_TOKEN_LENGTH + 1),
        )
        non_finite = template.replace(b'"NUMERIC_TOKEN"', b"1e309")
        self.assertFails(gm.ERROR_MALFORMED, oversized)
        self.assertFails(gm.ERROR_MALFORMED, non_finite)

    def test_rejects_source_reported_errors(self):
        self.assertFails(gm.ERROR_SOURCE_ERROR,
                         yahoo_payload(chart_error={"code": "Not Found",
                                                    "description": SECRET_REMOTE_TEXT}))

    def test_rejects_duplicate_json_keys(self):
        body = b'{"chart":{"error":null,"error":null,"result":[]}}'
        self.assertFails(gm.ERROR_MALFORMED, body)

    def test_requires_explicit_null_chart_error_field(self):
        payload = base_payload()
        payload["chart"].pop("error")
        self.assertFails(gm.ERROR_MALFORMED, encode(payload))

    def test_validates_timestamps_even_when_close_is_null(self):
        timestamps = [epoch(SESSIONS[0]), 10 ** 30, epoch(SESSIONS[-1])]
        self.assertFails(gm.ERROR_INVALID_TIME,
                         encode(base_payload(closes=[100.0, None, 102.0],
                                             timestamps=timestamps)))

    def test_request_streams_response_body(self):
        self.observation("^GSPC", yahoo_payload())
        self.assertIs(self.session.calls[0]["stream"], True)

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
        # Syntactically valid JSON numbers that are simply not usable prices:
        # the token parses, so the failure is a business value problem.
        for closes_json in ("[100.0, null, 0.0]", "[100.0, null, -1.0]",
                            "[100.0, null, -0.0]", "[100.0, null, 1e-1000]",
                            '[100.0, null, "102.0"]',
                            "[100.0, null, true]", "[100.0, null, [102.0]]"):
            with self.subTest(closes=closes_json[:24]):
                self.assertFails(gm.ERROR_INVALID_VALUE, yahoo_raw_payload(closes_json=closes_json))
        # The previous value is validated the same way.
        self.assertFails(gm.ERROR_INVALID_VALUE, yahoo_raw_payload(closes_json="[0.0, 102.0]"))
        # A maximum-length float token still parses; its value is judged on
        # business grounds rather than being rejected as structurally broken.
        at_limit = "0." + "0" * (gm.MAX_NUMERIC_TOKEN_LENGTH - 2)
        self.assertEqual(len(at_limit), gm.MAX_NUMERIC_TOKEN_LENGTH)
        self.assertFails(
            gm.ERROR_INVALID_VALUE,
            yahoo_raw_payload(closes_json=f"[100.0, null, {at_limit}]"),
        )

    def test_parse_stage_numeric_tokens_are_malformed_not_invalid_values(self):
        # A token the parser itself must refuse is a broken response, not a
        # business value: non-finite literals and over-long numeric tokens are
        # classified while decoding, before any close is interpreted.
        over_long = "9" * (gm.MAX_NUMERIC_TOKEN_LENGTH + 1)
        over_long_float = "1." + "0" * gm.MAX_NUMERIC_TOKEN_LENGTH
        for token in ("1e309", "-1e309", "1e1000", over_long, over_long_float):
            with self.subTest(token=token[:16]):
                self.assertFails(
                    gm.ERROR_MALFORMED,
                    yahoo_raw_payload(closes_json=f"[100.0, null, {token}]"),
                )
                self.assertFails(
                    gm.ERROR_MALFORMED,
                    yahoo_raw_payload(closes_json=f"[{token}, 102.0]"),
                )

    def test_parse_stage_limits_apply_in_every_numeric_position(self):
        # The parse-stage bound is a property of the decoder, so it must hold
        # wherever the token appears, including fields the adapter ignores.
        over_long = "9" * (gm.MAX_NUMERIC_TOKEN_LENGTH + 1)
        positions = {
            "timestamp": lambda: base_payload(
                closes=[1.0, 2.0],
                timestamps=[epoch(SESSIONS[0]), RAW_TOKEN],
            ),
            "source_time": lambda: base_payload(market_time=RAW_TOKEN),
            "ignored_field": lambda: dict(base_payload(), numeric_padding=RAW_TOKEN),
        }
        for name, build in positions.items():
            for token in ("1e309", over_long):
                with self.subTest(position=name, token=token[:16]):
                    self.assertFails(gm.ERROR_MALFORMED, raw_token_payload(build(), token))

    def test_malformed_parse_failures_never_retain_remote_content(self):
        # Parse errors are raised at the decoder boundary; the hostile literal
        # and the fixture's sentinel text must not survive on the exception.
        over_long = "1" * (gm.MAX_NUMERIC_TOKEN_LENGTH + 1)
        for token in ("1e309", over_long):
            with self.subTest(token=token[:16]):
                failure = self.assertFails(
                    gm.ERROR_MALFORMED,
                    yahoo_raw_payload(closes_json=f"[100.0, null, {token}]"),
                )
                self.assertNotIn(token, repr(failure))
                self.assertNotIn(SECRET_REMOTE_TEXT, repr(failure))

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
