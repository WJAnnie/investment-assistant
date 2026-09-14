"""AkShare adapter for the Hang Seng Tech Index."""

from datetime import date, datetime, timezone
import importlib
import math

from app.chan.models import KLine

from .models import Quote


class AkShareHKIndexProvider:
    """Fetch HSTECH spot and daily data from AkShare's Sina-backed endpoints."""

    SOURCE = "akshare_hk_index"
    SUPPORTED_CODES = {"HK.HSTECH", "HSTECH", "HSTECH.HK"}

    def __init__(self, ak_module=None, clock=None):
        self._ak = ak_module
        self._clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self._spot_cache = None
        self._spot_cache_ttl = 15.0

    def fetch(self, code: str):
        requested_code = self._validate_code(code)
        rows = self._spot_rows()
        for row in rows:
            if str(self._first(row, "code") or "").strip().upper() != "HSTECH":
                continue
            try:
                price = self._number(self._first(row, "price"), "price")
                change = self._number(self._first(row, "change"), "change")
                if price <= 0:
                    raise ValueError("price must be positive")
            except ValueError:
                raise ValueError("AkShare returned an invalid HSTECH quote") from None
            timestamp = self._timestamp(self._first(row, "time"))
            return Quote(
                requested_code,
                str(self._first(row, "name") or "HSTECH"),
                price,
                change,
                timestamp,
                source=self.SOURCE,
                market_time=timestamp,
            )
        return None

    def fetch_klines(self, code: str, **_kwargs):
        self._validate_code(code)
        ak = self._ak or importlib.import_module("akshare")
        frame = ak.stock_hk_index_daily_sina(symbol="HSTECH")
        rows = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else []
        lines = []
        for row in rows:
            try:
                timestamp = self._date_text(self._first(row, "time"))
                values = [
                    self._number(self._first(row, field), field)
                    for field in ("open", "high", "low", "close", "volume")
                ]
                if any(value <= 0 for value in values[:4]) or values[4] < 0 or values[1] < values[2]:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("AkShare returned an invalid HSTECH K-line row") from None
            lines.append(KLine(timestamp, *values[:4], values[4]))
        return sorted(lines, key=lambda line: line.time)

    @classmethod
    def _validate_code(cls, code):
        text = str(code or "").strip().upper()
        if text not in cls.SUPPORTED_CODES:
            raise ValueError("only HK.HSTECH is supported")
        return text

    def _spot_rows(self):
        now = self._clock_seconds()
        if self._spot_cache is not None and now - self._spot_cache[0] < self._spot_cache_ttl:
            return self._spot_cache[1]
        ak = self._ak or importlib.import_module("akshare")
        frame = ak.stock_hk_index_spot_sina()
        rows = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else []
        self._spot_cache = (now, rows)
        return rows

    @staticmethod
    def _first(row, field):
        aliases = {
            "code": ("代码", "code", "symbol"),
            "name": ("名称", "name"),
            "price": ("最新价", "最新", "price", "close"),
            "change": ("涨跌幅", "涨跌幅(%)", "change", "pct_chg"),
            "time": ("时间", "日期", "date", "datetime"),
            "open": ("开盘", "open"),
            "high": ("最高", "high"),
            "low": ("最低", "low"),
            "close": ("收盘", "close"),
            "volume": ("成交量", "volume", "vol"),
        }
        for alias in aliases[field]:
            if alias in row:
                return row[alias]
        return None

    @staticmethod
    def _number(value, field):
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{field} must be numeric") from None
        if not math.isfinite(result):
            raise ValueError(f"{field} must be finite")
        return result

    def _timestamp(self, value):
        if value is None or not str(value).strip():
            current = self._clock()
            if isinstance(current, datetime):
                return (current if current.tzinfo else current.replace(tzinfo=timezone.utc)).isoformat()
            return datetime.fromtimestamp(float(current), tz=timezone.utc).isoformat()
        return str(value).strip()

    def _clock_seconds(self):
        current = self._clock()
        if isinstance(current, datetime):
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            return current.timestamp()
        return float(current)

    @classmethod
    def _date_text(cls, value):
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value or "").strip()
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return date.fromisoformat(text).isoformat()
        raise ValueError("HSTECH date must be YYYY-MM-DD or YYYYMMDD")
