"""Keyless public observations for the eight approved overnight instruments.

This adapter only *reports* what a public endpoint said. It performs no retry,
keeps no cache, derives no trading action and never labels its output verified,
realtime or official: the Yahoo Chart endpoint behind it is unauthenticated and
may rate limit or change shape without notice. The Treasury instrument
additionally has a narrow, date-only FRED ``DGS10`` fallback that never pretends
to know an intraday time.

Trust boundaries enforced here:

* The symbol catalog is a fixed allowlist; a caller cannot request an arbitrary
  URL path segment.
* Every remote byte is treated as untrusted. Structural surprises, hostile
  values and confused responses fail closed with a fixed error code that never
  embeds remote text, headers, query strings or exception messages.
* The parsed payload is bounded by an *iterative* walk over container width,
  total node count and string length, and the daily series it must carry is
  bounded separately. Over-wide, over-populated or over-long responses fail
  closed without a recursive validator of their own.
* One request per symbol, no redirect following, no proxy or ``.netrc``
  credentials, a hard body cap and a hard timeout.
* Only `SourceObservation` leaves this module, so downstream code cannot read a
  raw response.
"""

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
import io
import json
import math
import re as _re
from types import MappingProxyType
from typing import NamedTuple
from urllib.parse import quote as _url_quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


YAHOO_CHART_ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/"
TIMEOUT_SECONDS = 10
MAX_BODY_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_NUMERIC_TOKEN_LENGTH = 64
MAX_CONTAINER_WIDTH = 128
MAX_JSON_NODES = 4096
MAX_STRING_LENGTH = 4096
MAX_SERIES_LENGTH = 32
_CHUNK_BYTES = 65536
_INVALID_TIMEOUT_MESSAGE = (
    "timeout must be a finite number greater than 0 and at most 10 seconds"
)
_INVALID_SESSION_MESSAGE = "session must allow trust_env to be disabled"
_USER_AGENT = "investment-assistant-public-trial/1.0"
_REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": _USER_AGENT,
}
_FRED_REQUEST_HEADERS = {
    "Accept": "text/csv",
    "User-Agent": _USER_AGENT,
}
# Sent through ``requests`` as separate parameters, never concatenated into the
# URL: the request URL must end at the encoded symbol.
_REQUEST_PARAMS = {"interval": "1d", "range": "5d"}

# FRED's CSV download endpoint. The endpoint and its single parameter are fixed
# constants, never caller input, so a FRED request has no variable part at all.
FRED_CSV_ENDPOINT = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_FRED_PARAMS = {"id": "DGS10"}
_FRED_HEADER_DATE = "observation_date"
_FRED_HEADER_LEGACY_DATE = "DATE"
_FRED_SERIES_HEADER = "DGS10"
_FRED_MISSING_MARKER = "."
FRED_SYMBOL = "^TNX"
_FRED_DATE_PATTERN = _re.compile(r"\A\d{4}-\d{2}-\d{2}\Z", _re.ASCII)
# A DGS10 value must look like a plain decimal number before ``float`` sees
# it: ``float`` alone would also accept underscore separators, padding
# whitespace and non-ASCII digits, none of which a real field contains.
_FRED_NUMBER_PATTERN = _re.compile(
    r"\A[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z", _re.ASCII)
# The one byte-order mark tolerated: a UTF-16 document cannot decode as
# UTF-8 and never reaches the CSV reader at all.
_UTF8_BOM = b"\xef\xbb\xbf"


# Timestamps outside this window are treated as a broken source rather than a
# real quote. The window avoids inventing a holiday calendar while still
# rejecting epoch zero, negative values and year-1000000 overflow.
MIN_SOURCE_EPOCH = 946684800    # 2000-01-01T00:00:00+00:00
MAX_SOURCE_EPOCH = 4102444800   # 2100-01-01T00:00:00+00:00


class CatalogRow(NamedTuple):
    category: str
    name: str
    value_unit: str
    timezone: str
    instrument_type: str


# The mutable mapping is deliberately anonymous: it is handed straight to the
# read-only view, so no module attribute (``_INSTRUMENTS`` or similar) exists
# for a caller to rewrite behind the provider's back. Rows are immutable named
# tuples, so the only thing that escapes this module is a frozen catalog.
INSTRUMENTS = MappingProxyType({
    "^GSPC": CatalogRow("us_equities", "标普500", "points", "America/New_York", "INDEX"),
    "^DJI": CatalogRow("us_equities", "道琼斯工业指数", "points", "America/New_York", "INDEX"),
    "^IXIC": CatalogRow("nasdaq", "纳斯达克综合指数", "points", "America/New_York", "INDEX"),
    "^SOX": CatalogRow("semiconductors", "费城半导体指数", "points", "America/New_York", "INDEX"),
    "^TNX": CatalogRow("us_treasury", "美国10年期国债收益率", "percent", "America/Chicago", "INDEX"),
    "DX-Y.NYB": CatalogRow("usd", "美元指数", "points", "America/New_York", "INDEX"),
    "GC=F": CatalogRow("gold", "COMEX黄金", "usd_per_ounce", "America/New_York", "FUTURE"),
    "CL=F": CatalogRow("oil", "WTI原油", "usd_per_barrel", "America/New_York", "FUTURE"),
})

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
    """Business validation for an already-parsed JSON number.

    Parse-stage limits (``_bounded_json_int``/``_bounded_json_float``) already
    rejected tokens that cannot become a finite number, so anything arriving
    here is a real number. A zero, negative, non-finite or non-numeric close is
    therefore a value problem (``INVALID_VALUE``), never a structural one.
    """
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
    # Classified here, at the parse boundary, so the code cannot drift with the
    # blanket ``except ValueError`` clause in ``_decode_json``.
    raise GlobalMarketDataError(ERROR_MALFORMED) from None


def _bounded_json_int(token):
    # ``token`` is remote text; only its length is inspected and it is never
    # echoed. ``from None`` keeps it out of any later traceback.
    if len(token) > MAX_NUMERIC_TOKEN_LENGTH:
        raise GlobalMarketDataError(ERROR_MALFORMED) from None
    return int(token)


def _bounded_json_float(token):
    if len(token) > MAX_NUMERIC_TOKEN_LENGTH:
        raise GlobalMarketDataError(ERROR_MALFORMED) from None
    value = float(token)
    # ``1e999`` parses to ``inf``; an unrepresentable price is a broken
    # response, not a business value, so it fails as MALFORMED here.
    if not math.isfinite(value):
        raise GlobalMarketDataError(ERROR_MALFORMED) from None
    return value


def _strict_object(pairs):
    # Last-wins duplicate keys would let a confused response shadow a value.
    keys = set()
    for key, _value in pairs:
        if key in keys:
            raise GlobalMarketDataError(ERROR_MALFORMED)
        keys.add(key)
    return dict(pairs)


def _exceeds_json_depth(text):
    """Scan structural brackets without treating bracket characters in strings."""
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return True
        elif character in "]}":
            depth -= 1
    return False


def _bounded_payload(payload):
    """Bound the parsed payload's *shape* without recursing.

    The parser hooks bound each numeric token and ``_exceeds_json_depth`` bounds
    nesting, but neither bounds breadth or length: one array element per byte
    would otherwise let a 1 MiB body describe an arbitrarily large structure.
    Limits are inclusive on purpose -- a container of exactly
    ``MAX_CONTAINER_WIDTH`` entries and a string of exactly
    ``MAX_STRING_LENGTH`` characters are legitimate -- and every breach is a
    fixed ``MALFORMED_RESPONSE`` that never echoes the offending text.

    Nodes are counted as the spec reads them: every container, every member key
    and every member value. The walk uses an explicit stack rather than calling
    itself, so a hostile shape cannot exhaust the interpreter stack.
    """
    nodes = 0
    pending = [payload]
    while pending:
        value = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise GlobalMarketDataError(ERROR_MALFORMED) from None
        value_type = type(value)
        if value_type is dict:
            if len(value) > MAX_CONTAINER_WIDTH:
                raise GlobalMarketDataError(ERROR_MALFORMED) from None
            for key, item in value.items():
                nodes += 1
                if nodes > MAX_JSON_NODES:
                    raise GlobalMarketDataError(ERROR_MALFORMED) from None
                # JSON object keys are always strings, but the length check is
                # applied without assuming that.
                if type(key) is str and len(key) > MAX_STRING_LENGTH:
                    raise GlobalMarketDataError(ERROR_MALFORMED) from None
                pending.append(item)
        elif value_type is list:
            if len(value) > MAX_CONTAINER_WIDTH:
                raise GlobalMarketDataError(ERROR_MALFORMED) from None
            pending.extend(value)
        elif value_type is str:
            if len(value) > MAX_STRING_LENGTH:
                raise GlobalMarketDataError(ERROR_MALFORMED) from None
    return payload


def _decode_json(body):
    malformed = False
    try:
        text = body.decode("utf-8")
    except (UnicodeDecodeError, AttributeError, MemoryError):
        malformed = True
    if malformed or _exceeds_json_depth(text):
        raise GlobalMarketDataError(ERROR_MALFORMED) from None

    try:
        payload = json.loads(text, object_pairs_hook=_strict_object,
                             parse_constant=_reject_constant,
                             parse_int=_bounded_json_int,
                             parse_float=_bounded_json_float)
    except GlobalMarketDataError:
        raise
    except (ValueError, TypeError, RecursionError, MemoryError):
        malformed = True
    if malformed:
        raise GlobalMarketDataError(ERROR_MALFORMED) from None
    return _bounded_payload(payload)


def _close(response):
    try:
        closer = getattr(response, "close", None)
    except (Exception, MemoryError):
        return
    if callable(closer):
        try:
            closer()
        except (Exception, MemoryError):
            pass


def _isolated_session():
    """A plain session; the trust flag is set by the guarded helper below.

    Writing ``trust_env`` here as well would be a second, unguarded copy of the
    same boundary: a ``MemoryError`` on that write could escape as itself rather
    than as the fixed failure the declaration promises.
    """
    return requests.Session()


def _disable_environment_credentials(session):
    """Force ``trust_env`` off, sealing any failure into one fixed message."""
    failed = False
    try:
        session.trust_env = False
        failed = session.trust_env is not False
    except (Exception, MemoryError):
        failed = True
    if failed:
        raise ValueError(_INVALID_SESSION_MESSAGE) from None


class _BoundedHttpProvider:
    """Shared transport boundary: one bounded, unauthenticated GET per call.

    An injected ``session`` is a trusted test-transport seam, not an extension
    point for carrying proxy, ``.netrc`` or other environment credentials. Only
    the endpoint, the parameters and the parsing differ between the sources; the
    10-second ceiling, the 1 MiB streamed cap, the redirect refusal and the
    fixed error codes are shared here so no source can grow a weaker path.
    """

    REQUEST_HEADERS = _REQUEST_HEADERS

    def __init__(self, session=None, timeout=TIMEOUT_SECONDS):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0 or timeout > TIMEOUT_SECONDS):
            raise ValueError(_INVALID_TIMEOUT_MESSAGE)
        configured_session = session if session is not None else _isolated_session()
        _disable_environment_credentials(configured_session)
        self.session = configured_session
        self.timeout = timeout

    def _get(self, url, params):
        transport_failed = False
        try:
            response = self.session.get(url, params=params,
                                        headers=dict(self.REQUEST_HEADERS),
                                        timeout=self.timeout, allow_redirects=False,
                                        stream=True)
        except GlobalMarketDataError:
            raise
        except (Exception, MemoryError):
            # Provider exception text can contain the URL or credentials.
            transport_failed = True
        if transport_failed:
            raise GlobalMarketDataError(ERROR_NETWORK) from None
        try:
            status_failed = False
            try:
                status = getattr(response, "status_code", None)
            except (Exception, MemoryError):
                status_failed = True
            if status_failed:
                raise GlobalMarketDataError(ERROR_NETWORK) from None
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
        stream_failed = False
        try:
            stream = response.iter_content(_CHUNK_BYTES)
        except (Exception, MemoryError):
            stream_failed = True
        if stream_failed:
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
        except (Exception, MemoryError):
            stream_failed = True
        if stream_failed:
            raise GlobalMarketDataError(ERROR_NETWORK) from None
        return b"".join(chunks)


class YahooGlobalMarketProvider(_BoundedHttpProvider):
    """Fetch one allowlisted instrument from Yahoo's keyless Chart endpoint.

    An injected ``session`` is a trusted test-transport seam, not an extension
    point for carrying proxy, ``.netrc`` or other environment credentials.
    """

    SOURCE = "yahoo_chart"

    def fetch(self, symbol):
        entry = catalog_entry(symbol)
        body = self._get(f"{YAHOO_CHART_ENDPOINT}{_url_quote(symbol, safe='')}",
                         dict(_REQUEST_PARAMS))
        return self._parse(body, symbol, entry)

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
        if meta.get("instrumentType") != entry.instrument_type:
            raise GlobalMarketDataError(ERROR_TYPE_MISMATCH) from None
        source_as_of = _epoch_to_iso(meta.get("regularMarketTime"))

        pairs = _strict_daily_pairs(series)
        previous_value, value, latest_epoch = pairs[-2][1], pairs[-1][1], pairs[-1][0]
        return SourceObservation(
            symbol=symbol,
            value=value,
            previous_value=previous_value,
            source_as_of=source_as_of,
            session_date=_session_date(latest_epoch, entry.timezone),
            source=self.SOURCE,
        )


class FredTreasuryProvider(_BoundedHttpProvider):
    """Fetch the Treasury yield from FRED's fixed ``DGS10`` CSV fallback.

    The fallback exists for one instrument only. The endpoint and its single
    parameter are module constants, so a caller cannot point this provider at
    another series, host or query; any other symbol is refused before a request
    is made. DGS10 is a *daily, delayed* series: it carries an observation date
    and no time of day, so ``source_as_of`` stays ``None`` and the observation
    date is reported verbatim instead of an invented intraday timestamp.
    """

    SOURCE = "fred_dgs10"
    REQUEST_HEADERS = _FRED_REQUEST_HEADERS

    def fetch(self, symbol):
        # A substituted instrument here would silently turn a Treasury yield
        # into another series, so the gate is an exact identity check and it
        # happens before the transport boundary is reached.
        if symbol != FRED_SYMBOL:
            raise GlobalMarketDataError(ERROR_UNSUPPORTED_SYMBOL) from None
        catalog_entry(symbol)  # The fallback symbol must stay in the catalog.
        body = self._get(FRED_CSV_ENDPOINT, dict(_FRED_PARAMS))
        pairs = _strict_fred_pairs(body)
        previous_value, value, latest_day = pairs[-2][1], pairs[-1][1], pairs[-1][0]
        return SourceObservation(
            symbol=symbol,
            value=value,
            previous_value=previous_value,
            source_as_of=None,
            session_date=latest_day.isoformat(),
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
    if len(closes) > MAX_SERIES_LENGTH or len(timestamps) > MAX_SERIES_LENGTH:
        # Both arrays are bounded independently: a series longer than the
        # adapter's whole horizon is a broken source, not a longer history.
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


def _strict_fred_pairs(body):
    """Return ``(date, value)`` pairs from a FRED ``DGS10`` CSV document.

    The document is remote text, so every stage fails closed with a fixed
    code and never echoes a row, header or exception message:

    * Only UTF-8 is decoded, with a UTF-8 BOM as the sole accepted prefix.
    * The header must be exactly ``observation_date,DGS10`` or ``DATE,DGS10``
      -- no extra, reordered or duplicated columns.
    * The NUL byte, ragged rows and any other CSV malformation are refused.
    * Every date must be a strict ``YYYY-MM-DD`` string naming a real
      calendar day, and dates must strictly increase across *every* row,
      including the ones whose value is missing.
    * ``.`` and the empty field are FRED's "no observation"; any other
      field must be a plain finite decimal number greater than zero.
    * At least two usable observations must survive.
    """
    malformed = False
    text = None
    try:
        if b"\x00" in body:
            # A NUL can hide inside a quoted field from every later stage.
            malformed = True
        else:
            text = body.decode("utf-8")
    except (UnicodeDecodeError, AttributeError, TypeError, MemoryError):
        # Wrong codec, a non-bytes document or an allocation failure are
        # all a broken source as far as this parser is concerned.
        malformed = True
    if malformed:
        raise GlobalMarketDataError(ERROR_MALFORMED) from None

    if text.startswith("\ufeff"):
        # The decoded UTF-8 BOM would otherwise corrupt the first header.
        text = text[1:]

    rows = []
    reader_failed = False
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        for row in reader:
            if len(row) != 2:
                reader_failed = True
                break
            rows.append((row[0], row[1]))
    except (csv.Error, ValueError, TypeError, MemoryError):
        reader_failed = True
    if reader_failed or not rows:
        # An empty document has no header to compare against either.
        raise GlobalMarketDataError(ERROR_MALFORMED) from None

    header_date, header_value = rows[0]
    if (header_date, header_value) not in (
            (_FRED_HEADER_DATE, _FRED_SERIES_HEADER),
            (_FRED_HEADER_LEGACY_DATE, _FRED_SERIES_HEADER)):
        raise GlobalMarketDataError(ERROR_MALFORMED) from None

    pairs = []
    previous_day = None
    for day_text, value_text in rows[1:]:
        if _FRED_DATE_PATTERN.match(day_text) is None:
            raise GlobalMarketDataError(ERROR_INVALID_TIME) from None
        parse_failed = False
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            parse_failed = True
        if parse_failed or day.isoformat() != day_text:
            # Impossible calendar days (``2026-02-30``) fail closed even
            # though the ISO parser silently normalizes some of them.
            raise GlobalMarketDataError(ERROR_INVALID_TIME) from None
        if previous_day is not None and day <= previous_day:
            # Duplicated, unsorted or backdated rows cannot identify
            # "latest", regardless of whether the value is missing.
            raise GlobalMarketDataError(ERROR_INVALID_TIME) from None
        previous_day = day
        if value_text in (_FRED_MISSING_MARKER, ""):
            # FRED marks "no observation" as ``.`` or an empty field.
            continue
        invalid_value = _FRED_NUMBER_PATTERN.match(value_text) is None
        if not invalid_value:
            conversion_failed = False
            try:
                value = float(value_text)
            except (OverflowError, ValueError):
                conversion_failed = True
            invalid_value = conversion_failed or not math.isfinite(value) or value <= 0
        if invalid_value:
            raise GlobalMarketDataError(ERROR_INVALID_VALUE) from None
        pairs.append((day, value))
    if len(pairs) < 2:
        raise GlobalMarketDataError(ERROR_INSUFFICIENT) from None
    return pairs
