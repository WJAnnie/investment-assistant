"""腾讯历史行情适配器：通过 AkShare 的 stock_zh_index_daily_tx 获取日线。"""

import importlib
import math
from datetime import date, datetime

from app.chan.models import KLine


class TencentHistoryProvider:
    """仅提供历史日 K 的腾讯备用源；异常行 fail-closed，不参与实时行情。"""

    _FIELDS = ("open", "high", "low", "close", "amount")
    _SZ_DIGIT_PREFIXES = ("0", "1", "2", "3")
    _SH_DIGIT_PREFIXES = ("5", "6")
    _CODE_ERROR = "code must use the SH./SZ. prefix or be a six-digit A-share/ETF code"

    def __init__(self, ak_module=None):
        self._ak = ak_module

    def fetch_klines(self, code, start_date=None, end_date=None, period="daily", adjust=""):
        """Return strictly validated daily K-lines sorted oldest first.

        ``adjust`` is accepted to mirror the collector call signature. The
        Tencent endpoint always serves 前复权 bars, so only "" and "qfq" are
        honoured; other adjustment modes fail closed instead of returning
        silently mismatched data.
        """
        if period != "daily":
            raise ValueError("TencentHistoryProvider supports only the daily period")
        if str(adjust or "") not in ("", "qfq"):
            raise ValueError("TencentHistoryProvider serves qfq bars only and cannot honour the requested adjustment")

        symbol = self._to_tencent_symbol(code)
        ak = self._ak or importlib.import_module("akshare")
        frame = ak.stock_zh_index_daily_tx(
            symbol=symbol,
            start_date=start_date or "",
            end_date=end_date or "",
        )
        rows = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else []
        lines = [self._to_kline(row) for row in rows]
        return sorted(lines, key=lambda line: line.time)

    @classmethod
    def _to_kline(cls, row):
        try:
            timestamp = cls._date_text(row["date"])
            values = [cls._number(row[field], field) for field in cls._FIELDS]
            if any(value <= 0 for value in values[:4]) or values[4] < 0:
                raise ValueError("prices must be positive and amount must be non-negative")
            if values[1] < values[2]:
                raise ValueError("high must not be below low")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid Tencent K-line row: {row!r}") from exc
        return KLine(timestamp, values[0], values[1], values[2], values[3], values[4])

    @classmethod
    def _to_tencent_symbol(cls, code):
        text = str(code or "").strip().upper()
        if text.startswith(("SH.", "SZ.")):
            exchange, symbol = text[:2], text[3:]
        elif cls._is_six_digits(text):
            exchange, symbol = cls._infer_exchange(text), text
        else:
            raise ValueError(cls._CODE_ERROR)
        if not cls._is_six_digits(symbol):
            raise ValueError(cls._CODE_ERROR)
        return f"{exchange.lower()}{symbol}"

    @classmethod
    def _infer_exchange(cls, symbol):
        if symbol.startswith(cls._SZ_DIGIT_PREFIXES):
            return "SZ"
        if symbol.startswith(cls._SH_DIGIT_PREFIXES):
            return "SH"
        raise ValueError("cannot infer exchange from the six-digit code")

    @staticmethod
    def _is_six_digits(value):
        return len(value) == 6 and value.isascii() and value.isdigit()

    @staticmethod
    def _number(value, field):
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{field} must be numeric") from None
        if not math.isfinite(result):
            raise ValueError(f"{field} must be finite")
        return result

    @staticmethod
    def _date_text(value):
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value or "").strip()
        if len(text) == 8 and text.isascii() and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return date.fromisoformat(text).isoformat()
        raise ValueError("Tencent date must be YYYY-MM-DD or YYYYMMDD")
