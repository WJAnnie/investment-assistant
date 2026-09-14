from collections.abc import Mapping
import os

import requests

from app.chan.models import KLine
from .models import Quote


class TushareError(RuntimeError):
    """Base error for the optional Tushare data source."""


class TushareConfigurationError(TushareError):
    """Raised when Tushare is used without a token."""


class TushareAPIError(TushareError):
    """Raised when Tushare rejects a request or returns malformed data."""


class TushareProvider:
    """Tushare Pro REST adapter for realtime and historical A-share/ETF bars."""

    DEFAULT_ENDPOINT = "https://api.tushare.pro"
    _ETF_PREFIXES = ("15", "16", "50", "51", "56", "58")
    _REALTIME_PERIODS = {"1", "5", "15", "30", "60"}

    def __init__(self, token=None, session=None, endpoint=None, timeout=10):
        self.token = token or os.getenv("TUSHARE_TOKEN")
        self.session = session or requests.Session()
        self.endpoint = endpoint or self.DEFAULT_ENDPOINT
        self.timeout = timeout

    def query(self, api_name, params=None, fields=""):
        if not self.token:
            raise TushareConfigurationError("TUSHARE_TOKEN is required")
        if not isinstance(api_name, str) or not api_name.strip():
            raise ValueError("api_name must be a non-empty string")
        if params is not None and not isinstance(params, Mapping):
            raise TypeError("params must be a mapping")

        payload = {
            "api_name": api_name,
            "token": self.token,
            "params": dict(params or {}),
            "fields": fields,
        }
        try:
            response = self.session.post(
                self.endpoint,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TushareAPIError(f"Tushare request failed: {exc}") from exc

        if not isinstance(result, Mapping) or result.get("code") != 0:
            message = result.get("msg") if isinstance(result, Mapping) else "invalid response"
            raise TushareAPIError(str(message or "Tushare request rejected"))

        data = result.get("data")
        if not isinstance(data, Mapping):
            raise TushareAPIError("Tushare response has no data")
        columns = data.get("fields")
        items = data.get("items")
        if not isinstance(columns, list) or not isinstance(items, list):
            raise TushareAPIError("Tushare response has invalid fields/items")

        rows = []
        for item in items:
            if not isinstance(item, list) or len(item) != len(columns):
                raise TushareAPIError("Tushare response row does not match fields")
            rows.append(dict(zip(columns, item)))
        return rows

    def fetch(self, code):
        """Return a realtime quote when available, then fall back to the latest daily close."""
        ts_code, api_name = self._to_ts_code(code)
        if not self.token:
            raise TushareConfigurationError("TUSHARE_TOKEN is required")

        realtime_api = self._realtime_api(api_name)
        try:
            realtime_rows = self.query(
                realtime_api,
                params={"ts_code": ts_code, "freq": "1MIN"},
                fields="ts_code,time,open,high,low,close,vol",
            )
        except TushareAPIError:
            realtime_rows = []

        realtime_quote = self._to_realtime_quote(code, realtime_rows)
        if realtime_quote is not None:
            return realtime_quote

        rows = self.query(
            api_name,
            params={"ts_code": ts_code},
            fields="ts_code,trade_date,close,pct_chg",
        )
        if not rows:
            return None

        latest = max(rows, key=lambda row: str(row.get("trade_date", "")))
        try:
            price = float(latest["close"])
            change = float(latest.get("pct_chg") or 0.0)
            timestamp = str(latest["trade_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TushareAPIError("Tushare returned an invalid latest quote") from exc
        return Quote(str(code).strip(), str(code).strip(), price, change, timestamp)

    def fetch_klines(
        self,
        code,
        start_date=None,
        end_date=None,
        period="daily",
        adjust="",
    ):
        ts_code, api_name = self._to_ts_code(code)
        params = {"ts_code": ts_code}
        if period in self._REALTIME_PERIODS:
            query_name = self._realtime_api(api_name)
            params["freq"] = f"{period}MIN"
            fields = "ts_code,time,open,high,low,close,vol"
            timestamp_field = "time"
        elif period == "daily":
            query_name = api_name
            if start_date:
                params["start_date"] = start_date
            if end_date:
                params["end_date"] = end_date
            fields = "ts_code,trade_date,open,high,low,close,vol"
            timestamp_field = "trade_date"
        else:
            raise ValueError(
                "TushareProvider supports daily or realtime minute periods only"
            )

        rows = self.query(query_name, params=params, fields=fields)
        lines = []
        for row in rows:
            try:
                values = [
                    float(row[field])
                    for field in ("open", "high", "low", "close", "vol")
                ]
                timestamp = str(row[timestamp_field])
                if not timestamp or timestamp == "None":
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise TushareAPIError("Tushare returned an invalid K-line row") from exc
            lines.append(KLine(timestamp, *values[:4], values[4]))
        return sorted(lines, key=lambda line: line.time)

    @classmethod
    def _realtime_api(cls, daily_api):
        return "rt_etf_min_daily" if daily_api == "fund_daily" else "rt_min_daily"

    @staticmethod
    def _to_realtime_quote(code, rows):
        if not rows:
            return None
        latest = max(rows, key=lambda row: str(row.get("time", "")))
        try:
            timestamp = str(latest["time"])
            price = float(latest["close"])
        except (KeyError, TypeError, ValueError):
            return None
        if not timestamp or timestamp == "None":
            return None
        return Quote(str(code).strip(), str(code).strip(), price, 0.0, timestamp)

    @classmethod
    def _to_ts_code(cls, code):
        text = str(code or "").strip().upper()
        if text.startswith("SH.") or text.startswith("SZ."):
            text = f"{text[3:]}.{text[:2]}"
        if text.endswith(".HK") or text.startswith("HK."):
            raise ValueError("TushareProvider currently supports A-share and ETF codes only")
        if text.endswith(".SH") or text.endswith(".SZ"):
            exchange = text[-2:]
            symbol = text[:-3]
        elif text.isdigit() and len(text) == 6:
            symbol = text
            exchange = cls._exchange_for(symbol)
        else:
            raise ValueError("code must be a six-digit A-share or ETF code")

        api_name = "fund_daily" if symbol.startswith(cls._ETF_PREFIXES) else "daily"
        return f"{symbol}.{exchange}", api_name

    @staticmethod
    def _exchange_for(symbol):
        if symbol.startswith(("6", "5")):
            return "SH"
        if symbol.startswith(("0", "1", "2", "3")):
            return "SZ"
        raise ValueError("cannot infer exchange from code")
