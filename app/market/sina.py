from datetime import datetime, timezone
import math
import re
import time

import requests

from .models import Quote


class SinaProvider:
    """新浪公开行情快照适配器，用作独立于 AkShare 的实时备用源。"""

    DEFAULT_ENDPOINT = "https://hq.sinajs.cn/list="
    _HEADERS = {"Referer": "https://finance.sina.com.cn/"}

    def __init__(self, session=None, endpoint=None, timeout=10, clock=None):
        self.session = session or requests.Session()
        self.endpoint = endpoint or self.DEFAULT_ENDPOINT
        self.timeout = timeout
        self._clock = clock or time.time

    def fetch(self, code):
        requested_code = str(code or "").strip()
        sina_code, exchange, symbol = self._to_sina_code(requested_code)
        response = self.session.get(
            f"{self.endpoint}{sina_code}",
            headers=self._HEADERS,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = self._response_text(response)
        match = re.search(
            rf'hq_str_{re.escape(sina_code)}="([^"]*)"',
            body,
        )
        if match is None:
            return None

        values = match.group(1).split(",")
        try:
            name, price, change = self._parse_values(values, exchange, symbol)
        except (TypeError, ValueError):
            return None
        if not name or not math.isfinite(price) or price < 0 or not math.isfinite(change):
            return None

        timestamp = datetime.fromtimestamp(
            self._clock(), tz=timezone.utc
        ).isoformat()
        return Quote(requested_code, name, price, change, timestamp)

    @staticmethod
    def _response_text(response):
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            text = content.decode("utf-8", errors="replace")
            if "�" in text:
                text = content.decode("gbk", errors="replace")
            return text
        return str(getattr(response, "text", ""))

    @staticmethod
    def _parse_values(values, exchange, symbol):
        if not values or not values[0].strip():
            raise ValueError("Sina returned an empty quote")
        if SinaProvider._is_index(exchange, symbol):
            if len(values) < 4:
                raise ValueError("Sina returned an incomplete index quote")
            return values[0].strip(), float(values[1]), float(values[3])

        if len(values) < 4:
            raise ValueError("Sina returned an incomplete stock quote")
        price = float(values[3])
        previous_close = float(values[2])
        change = 0.0 if previous_close == 0 else (price - previous_close) / previous_close * 100
        return values[0].strip(), price, change

    @staticmethod
    def _is_index(exchange, symbol):
        return symbol.startswith("399") or (exchange == "sh" and symbol.startswith("000"))

    @staticmethod
    def _to_sina_code(code):
        text = str(code or "").strip().upper()
        if text.startswith("HK.") or text.endswith(".HK"):
            raise ValueError("SinaProvider currently supports A-share and ETF codes only")

        exchange = None
        if text.startswith(("SH.", "SZ.")):
            exchange, text = text[:2].lower(), text[3:]
        elif text.endswith((".SH", ".SZ")):
            exchange, text = text[-2:].lower(), text[:-3]
        elif text.isdigit() and len(text) == 6:
            exchange = SinaProvider._exchange_for(text)
        else:
            raise ValueError("code must be a six-digit A-share or ETF code")

        if not text.isdigit() or len(text) != 6:
            raise ValueError("code must be a six-digit A-share or ETF code")
        prefix = "s_" if SinaProvider._is_index(exchange, text) else ""
        return f"{prefix}{exchange}{text}", exchange, text

    @staticmethod
    def _exchange_for(symbol):
        if symbol.startswith(("15", "16", "0", "1", "2", "3")):
            return "sz"
        if symbol.startswith(("5", "6")):
            return "sh"
        raise ValueError("cannot infer exchange from code")
