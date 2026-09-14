from datetime import datetime
from collections.abc import Mapping
import math

from app.chan.models import KLine
from .cache import MarketCache
from .models import Quote
from .validator import validate_quote


class MarketDataUnavailable(RuntimeError):
    """Raised when no configured market data source returns a valid quote."""


class MarketCollector:
    """统一行情采集入口，带缓存和主/备用数据源降级。"""

    def __init__(self, primary=None, fallback=None, cache=None, cache_expire=300):
        self.primary = primary
        self.fallback = fallback
        self.cache = cache or MarketCache()
        self.cache_expire = cache_expire
        self.last_errors = {}

    def get_quote(self, code: str) -> Quote:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty string")

        cache_key = f"quote:{code}"
        cached = self.cache.get(cache_key, self.cache_expire)
        if cached is not None:
            return cached

        errors = []
        for provider in self._iter_providers():
            provider_name = type(provider).__name__
            try:
                data = provider.fetch(code)
            except Exception as exc:
                errors.append(f"{provider_name}: {exc}")
                continue

            quote = self._to_quote(data)
            if quote is None:
                errors.append(f"{provider_name}: invalid or empty quote")
                continue
            self.cache.set(cache_key, quote)
            self.last_errors[cache_key] = []
            return quote

        self.last_errors[cache_key] = errors
        raise MarketDataUnavailable(self._failure_message("quote", code, errors))

    def get_klines(self, code: str, **kwargs):
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty string")

        errors = []
        cache_key = f"klines:{code}"
        for provider in self._iter_providers():
            provider_name = type(provider).__name__
            try:
                lines = provider.fetch_klines(code, **kwargs)
            except Exception as exc:
                errors.append(f"{provider_name}: {exc}")
                continue
            if self._valid_klines(lines):
                self.last_errors[cache_key] = []
                return lines
            errors.append(f"{provider_name}: invalid or empty K-lines")

        self.last_errors[cache_key] = errors
        raise MarketDataUnavailable(self._failure_message("K-lines", code, errors))

    @staticmethod
    def _failure_message(data_kind, code, errors):
        message = f"no valid {data_kind} available for {code}"
        if errors:
            message += "; providers failed: " + " | ".join(errors)
        return message

    def _iter_providers(self):
        for configured in (self.primary, self.fallback):
            if configured is None:
                continue
            if isinstance(configured, (list, tuple)):
                for provider in configured:
                    if provider is not None:
                        yield provider
            else:
                yield configured

    @staticmethod
    def _valid_klines(lines):
        if not isinstance(lines, list) or len(lines) < 2:
            return False
        for line in lines:
            if not isinstance(line, KLine):
                return False
            values = (line.open, line.high, line.low, line.close, line.volume)
            if not all(math.isfinite(float(value)) for value in values):
                return False
            if line.high < line.low:
                return False
        return True

    @staticmethod
    def _to_quote(data):
        if isinstance(data, Quote):
            return data if validate_quote(data) else None
        if not isinstance(data, Mapping):
            return None
        try:
            quote = Quote(
                code=str(data["code"]),
                name=str(data["name"]),
                price=float(data["price"]),
                change=float(data["change"]),
                timestamp=str(data.get("timestamp") or datetime.now().isoformat()),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return quote if validate_quote(quote) else None
