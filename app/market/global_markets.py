"""Keyless public observations for the eight approved overnight instruments.

This adapter only *reports* what a public endpoint said. It performs no retry,
keeps no cache, derives no trading action and never labels its output verified,
realtime or official: the Yahoo Chart endpoint behind it is unauthenticated and
may rate limit or change shape without notice.

Trust boundaries enforced here:

* The symbol catalog is a fixed allowlist; a caller cannot request an arbitrary
  URL path segment.
* Every remote byte is treated as untrusted. Structural surprises, hostile
  values and confused responses fail closed with a fixed error code that never
  embeds remote text, headers, query strings or exception messages.
* One request per symbol, no redirect following, no proxy or ``.netrc``
  credentials, a hard body cap and a hard timeout.
* Only `SourceObservation` leaves this module, so downstream code cannot read a
  raw response.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import json
import math
from urllib.parse import quote as _url_quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


YAHOO_CHART_ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/"
TIMEOUT_SECONDS = 10
MAX_BODY_BYTES = 1024 * 1024
_CHUNK_BYTES = 65536
_REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "investment-assistant-public-trial/1.0",
}
# Sent through ``requests`` as separate parameters, never concatenated into the
# URL: the request URL must end at the encoded symbol.
_REQUEST_PARAMS = {"interval": "1d", "range": "5d"}

# Timestamps outside this window are treated as a broken source rather than a
# real quote. The window avoids inventing a holiday calendar while still
# rejecting epoch zero, negative values and year-1000000 overflow.
MIN_SOURCE_EPOCH = 946684800    # 2000-01-01T00:00:00+00:00
MAX_SOURCE_EPOCH = 4102444800   # 2100-01-01T00:00:00+00:00

# symbol -> (category, user-facing name, value unit, market timezone, source type)
INSTRUMENTS = {
    "^GSPC": ("us_equities", "标普500", "points", "America/New_York", "INDEX"),
    "^DJI": ("us_equities", "道琼斯工业指数", "points", "America/New_York", "INDEX"),
    "^IXIC": ("nasdaq", "纳斯达克综合指数", "points", "America/New_York", "INDEX"),
    "^SOX": ("semiconductors", "费城半导体指数", "points", "America/New_York", "INDEX"),
    "^TNX": ("us_treasury", "美国10年期国债收益率", "percent", "America/Chicago", "INDEX"),
    "DX-Y.NYB": ("usd", "美元指数", "points", "America/New_York", "INDEX"),
    "GC=F": ("gold", "COMEX黄金", "usd_per_ounce", "America/New_York", "FUTURE"),
    "CL=F": ("oil", "WTI原油", "usd_per_barrel", "America/New_York", "FUTURE"),
}

CATEGORY, NAME, VALUE_UNIT, TIMEZONE, INSTRUMENT_TYPE = range(5)

ERROR_UNSUPPORTED_SYMBOL = "UNSUPPORTED_SYMBOL"
ERROR_NETWORK = "NETWORK_UNAVAILABLE"
ERROR_REDIRECT = "REDIRECT_BLOCKED"
ERROR_RATE_LIMITED = "RATE_LIMITED"
ERROR_HTTP_STATUS = "HTTP_STATUS_ERROR"
ERROR_TOO_LARGE = "BODY_TOO_LARGE"
ERROR_MALFORMED = "MALFORMED_RESPONSE"
ERROR_SOURCE_ERROR = "SOURCE_ERROR"
ERROR_SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
ERROR_TYPE_MISMATCH = "INSTRUMENT_TYPE_MISMATCH"
ERROR_INSUFFICIENT = "INSUFFICIENT_HISTORY"
ERROR_INVALID_VALUE = "INVALID_VALUE"
ERROR_INVALID_TIME = "INVALID_TIME"

ERROR_CODES = frozenset({
    ERROR_UNSUPPORTED_SYMBOL, ERROR_NETWORK, ERROR_REDIRECT, ERROR_RATE_LIMITED,
    ERROR_HTTP_STATUS, ERROR_TOO_LARGE, ERROR_MALFORMED, ERROR_SOURCE_ERROR,
    ERROR_SYMBOL_MISMATCH, ERROR_TYPE_MISMATCH, ERROR_INSUFFICIENT,
    ERROR_INVALID_VALUE, ERROR_INVALID_TIME,
})


class GlobalMarketDataError(Exception):
    """Fixed-code failure. The message is the code, never remote content."""

    def __init__(self, code):
        if code not in ERROR_CODES:
            raise ValueError("GlobalMarketDataError requires a catalogued code")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SourceObservation:
    """One successfully parsed public observation. It is data, not a signal."""

    symbol: str
    value: float
    previous_value: float
    source_as_of: str | None
    session_date: str
    source: str


def is_supported(symbol):
    return type(symbol) is str and symbol in INSTRUMENTS


def catalog_entry(symbol):
    """Return the frozen catalog row, failing closed on any other symbol."""
    if not is_supported(symbol):
        raise GlobalMarketDataError(ERROR_UNSUPPORTED_SYMBOL) from None
    return INSTRUMENTS[symbol]


@lru_cache(maxsize=None)
def _market_zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Fail closed instead of inventing a fixed offset for an unknown zone.
        raise GlobalMarketDataError(ERROR_INVALID_TIME) from None


def _epoch_to_iso(seconds):
    if type(seconds) is not int or not MIN_SOURCE_EPOCH <= seconds <= MAX_SOURCE_EPOCH:
        raise GlobalMarketDataError(ERROR_INVALID_TIME) from None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _session_date(seconds, zone_name):
    _epoch_to_iso(seconds)
    return datetime.fromtimestamp(seconds, tz=_market_zone(zone_name)).date().isoformat()


def _number(value):
    """Accept only a real JSON number that is finite and strictly positive."""
    if type(value) not in (int, float):
        raise GlobalMarketDataError(ERROR_INVALID_VALUE) from None
    conversion_failed = False
    try:
        result = float(value)
    except (OverflowError, ValueError):
        # Arbitrarily large JSON integers cannot become a usable price. Raise
        # outside this handler so the conversion exception is not retained.
        conversion_failed = True
    if conversion_failed:
        raise GlobalMarketDataError(ERROR_INVALID_VALUE) from None
    if not math.isfinite(result) or result <= 0:
        raise GlobalMarketDataError(ERROR_INVALID_VALUE) from None
    return result


def _reject_constant(_name):
    # ``NaN``/``Infinity`` are not valid JSON and are never a market price.
    raise GlobalMarketDataError(ERROR_MALFORMED)


def _strict_object(pairs):
    # Last-wins duplicate keys would let a confused response shadow a value.
    keys = set()
    for key, _value in pairs:
        if key in keys:
            raise GlobalMarketDataError(ERROR_MALFORMED)
        keys.add(key)
    return dict(pairs)


def _decode_json(body):
    malformed = False
    try:
        text = body.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        malformed = True
    if malformed:
        raise GlobalMarketDataError(ERROR_MALFORMED) from None

    try:
        payload = json.loads(text, object_pairs_hook=_strict_object,
                             parse_constant=_reject_constant)
    except GlobalMarketDataError:
        raise
    except (ValueError, TypeError, RecursionError):
        malformed = True
    if malformed:
        raise GlobalMarketDataError(ERROR_MALFORMED) from None
    return payload


def _close(response):
    closer = getattr(response, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _isolated_session():
    session = requests.Session()
    # No environment proxy and no implicit .netrc credentials.
    session.trust_env = False
    return session


class YahooGlobalMarketProvider:
    """Fetch one allowlisted instrument from Yahoo's keyless Chart endpoint."""

    SOURCE = "yahoo_chart"

    def __init__(self, session=None, timeout=TIMEOUT_SECONDS):
        self.session = session if session is not None else _isolated_session()
        self.timeout = timeout

    def fetch(self, symbol):
        entry = catalog_entry(symbol)
        body = self._get(f"{YAHOO_CHART_ENDPOINT}{_url_quote(symbol, safe='')}",
                         dict(_REQUEST_PARAMS))
        return self._parse(body, symbol, entry)

    def _get(self, url, params):
        transport_failed = False
        try:
            response = self.session.get(url, params=params,
                                        headers=dict(_REQUEST_HEADERS),
                                        timeout=self.timeout, allow_redirects=False,
                                        stream=True)
        except GlobalMarketDataError:
            raise
        except Exception:
            # Provider exception text can contain the URL or credentials.
            transport_failed = True
        if transport_failed:
            raise GlobalMarketDataError(ERROR_NETWORK) from None
        try:
            status = getattr(response, "status_code", None)
            if type(status) is not int:
                raise GlobalMarketDataError(ERROR_NETWORK) from None
            if 300 <= status < 400:
                raise GlobalMarketDataError(ERROR_REDIRECT) from None
            if status == 429:
                raise GlobalMarketDataError(ERROR_RATE_LIMITED) from None
            if status != 200:
                raise GlobalMarketDataError(ERROR_HTTP_STATUS) from None
            return self._read_body(response)
        finally:
            _close(response)

    @staticmethod
    def _read_body(response):
        chunks = []
        total = 0
        try:
            stream = response.iter_content(_CHUNK_BYTES)
        except Exception:
            raise GlobalMarketDataError(ERROR_NETWORK) from None
        try:
            for chunk in stream:
                if type(chunk) not in (bytes, bytearray):
                    raise GlobalMarketDataError(ERROR_MALFORMED) from None
                total += len(chunk)
                if total > MAX_BODY_BYTES:
                    raise GlobalMarketDataError(ERROR_TOO_LARGE) from None
                chunks.append(bytes(chunk))
        except GlobalMarketDataError:
            raise
        except Exception:
            raise GlobalMarketDataError(ERROR_NETWORK) from None
        return b"".join(chunks)

    def _parse(self, body, symbol, entry):
        payload = _decode_json(body)
        if type(payload) is not dict:
            raise GlobalMarketDataError(ERROR_MALFORMED) from None
        chart = payload.get("chart")
        if type(chart) is not dict:
            raise GlobalMarketDataError(ERROR_MALFORMED) from None
        if "error" not in chart:
            raise GlobalMarketDataError(ERROR_MALFORMED) from None
        if chart["error"] is not None:
            raise GlobalMarketDataError(ERROR_SOURCE_ERROR) from None
        result = chart.get("result")
        if type(result) is not list or len(result) != 1 or type(result[0]) is not dict:
            raise GlobalMarketDataError(ERROR_MALFORMED) from None
        series = result[0]

        meta = series.get("meta")
        if type(meta) is not dict:
            raise GlobalMarketDataError(ERROR_MALFORMED) from None
        # A silently substituted look-alike instrument is worse than no data.
        if meta.get("symbol") != symbol:
            raise GlobalMarketDataError(ERROR_SYMBOL_MISMATCH) from None
        if meta.get("instrumentType") != entry[INSTRUMENT_TYPE]:
            raise GlobalMarketDataError(ERROR_TYPE_MISMATCH) from None
        source_as_of = _epoch_to_iso(meta.get("regularMarketTime"))

        pairs = _strict_daily_pairs(series)
        previous_value, value, latest_epoch = pairs[-2][1], pairs[-1][1], pairs[-1][0]
        return SourceObservation(
            symbol=symbol,
            value=value,
            previous_value=previous_value,
            source_as_of=source_as_of,
            session_date=_session_date(latest_epoch, entry[TIMEZONE]),
            source=self.SOURCE,
        )


def _strict_daily_pairs(series):
    """Return the validated ``(epoch, close)`` pairs in source order."""
    indicators = series.get("indicators")
    if type(indicators) is not dict:
        raise GlobalMarketDataError(ERROR_MALFORMED) from None
    quote = indicators.get("quote")
    if type(quote) is not list or len(quote) != 1 or type(quote[0]) is not dict:
        raise GlobalMarketDataError(ERROR_MALFORMED) from None
    closes = quote[0].get("close")
    timestamps = series.get("timestamp")
    if type(closes) is not list or type(timestamps) is not list:
        raise GlobalMarketDataError(ERROR_MALFORMED) from None
    if len(closes) != len(timestamps):
        raise GlobalMarketDataError(ERROR_MALFORMED) from None

    pairs = []
    previous_epoch = None
    for seconds, close in zip(timestamps, closes):
        _epoch_to_iso(seconds)  # Validate timestamps even when the close is null.
        if previous_epoch is not None and seconds <= previous_epoch:
            # Duplicated or unsorted history cannot identify "latest".
            raise GlobalMarketDataError(ERROR_INVALID_TIME) from None
        previous_epoch = seconds
        if close is None:
            # A closed session legitimately has no close price.
            continue
        value = _number(close)
        pairs.append((seconds, value))
    if len(pairs) < 2:
        raise GlobalMarketDataError(ERROR_INSUFFICIENT) from None
    return pairs
