"""Portfolio valuation routing and honest freshness classification."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.market.models import Quote

from .models import Valuation


@dataclass(frozen=True)
class _ValuationTarget:
    code: str
    valuation_mode: str
    venue: str = "cn"


class PortfolioValuationRouter:
    MAINLAND_TZ = ZoneInfo("Asia/Shanghai")
    HSTECH_ALIASES = frozenset({"HK.HSTECH", "HSTECH", "HSTECH.HK"})
    _MAINLAND_HOLIDAYS = frozenset({
        # SSE 2026 trading-holiday notice:
        # https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml
        date(2026, 1, 1), date(2026, 1, 2),
        date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
        date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 23),
        date(2026, 4, 6),
        date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),
        date(2026, 6, 19), date(2026, 9, 25),
        date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
        date(2026, 10, 6), date(2026, 10, 7),
    })

    def __init__(self, exchange_collector, nav_provider, hk_provider, clock=None):
        self.exchange_collector = exchange_collector
        self.nav_provider = nav_provider
        self.hk_provider = hk_provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def value_holding(self, holding):
        target = self._target_for(holding)
        if self._is_hstech(target.code):
            quote = self.hk_provider.fetch(target.code)
            provider = self.hk_provider
        elif target.valuation_mode == "exchange":
            quote = self.exchange_collector.get_quote(target.code)
            provider = self.exchange_collector
        elif target.valuation_mode in {"fund_nav", "qdii_nav"}:
            quote = self.nav_provider.fetch(target.code)
            provider = self.nav_provider
        else:
            raise ValueError(f"unsupported valuation mode: {holding.valuation_mode}")
        if quote is None:
            raise ValueError(f"no quote available for {holding.code}")
        return self._to_valuation(target, quote, provider)

    def value_portfolio(self, config, include_hstech=True):
        valuations = {}
        holdings = [holding for account in config.accounts for holding in account.holdings]
        for holding in holdings:
            code = self._canonical_code(holding.code)
            if code in valuations:
                continue
            try:
                valuations[code] = self.value_holding(holding)
            except Exception as exc:
                valuations[code] = self._failed(code, self._provider_for(holding), exc)

        hstech_code = "HK.HSTECH"
        if include_hstech and hstech_code not in valuations:
            try:
                quote = self.hk_provider.fetch(hstech_code)
                if quote is None:
                    raise ValueError("no quote available for HK.HSTECH")
                valuations[hstech_code] = self._to_valuation(
                    _ValuationTarget(hstech_code, "exchange", "hk"),
                    quote,
                    self.hk_provider,
                )
            except Exception as exc:
                valuations[hstech_code] = self._failed(hstech_code, self.hk_provider, exc)
        return valuations

    def get_klines_for_holding(self, holding, **kwargs):
        """Fetch history through the same venue boundary used for valuation."""
        return self._fetch_history(self._target_for(holding), **kwargs)

    def get_klines(self, code, valuation_mode="exchange", market="CN", **kwargs):
        """Fetch history for an explicit code, including the HSTECH benchmark."""
        canonical = self._canonical_code(code)
        venue = "hk" if str(market).upper() == "HK" or self._is_hstech(canonical) else "cn"
        target = _ValuationTarget(canonical, valuation_mode, venue)
        return self._fetch_history(target, **kwargs)

    def _fetch_history(self, target, **kwargs):
        provider = self._history_provider_for(target)
        if provider is self.exchange_collector:
            return provider.get_klines(target.code, **kwargs)
        return provider.fetch_klines(target.code, **kwargs)

    def _to_valuation(self, holding, quote, provider=None):
        target = holding if isinstance(holding, _ValuationTarget) else self._target_for(holding)
        if not isinstance(quote, Quote):
            raise ValueError("provider returned an invalid quote")
        try:
            quote_code = self._canonical_code(quote.code)
            if quote_code != target.code:
                raise ValueError("provider returned a quote for a different code")
            price = self._decimal(quote.price, "price")
            change = self._decimal(quote.change, "change")
            if price < 0:
                raise ValueError("price must be non-negative")
            source_time = quote.market_time or quote.timestamp
            as_of = self._parse_timestamp(source_time)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("provider returned an invalid quote timestamp or value") from exc

        now = self._now()
        if as_of > now + timedelta(minutes=1):
            raise ValueError("provider returned a source timestamp in the future")
        freshness = self._freshness(target, as_of, now)
        source = quote.source or self._provider_name(provider)
        return Valuation(target.code, price, change, as_of, source, freshness)

    def _freshness(self, target, as_of, now):
        if target.valuation_mode == "exchange":
            return "stale" if self._exchange_is_stale(as_of, now, target.venue) else "fresh"
        if target.valuation_mode == "fund_nav":
            current_date = now.astimezone(self.MAINLAND_TZ).date()
            return "fresh" if self._is_recent_mainland_business_day(as_of.date(), current_date) else "stale"
        if target.valuation_mode == "qdii_nav":
            current_date = now.astimezone(self.MAINLAND_TZ).date()
            return "fresh" if self._is_recent_mainland_business_day(as_of.date(), current_date) else "lagged"
        raise ValueError(f"unsupported valuation mode: {target.valuation_mode}")

    @classmethod
    def _exchange_is_stale(cls, as_of, now, venue="cn"):
        local_now = now.astimezone(cls.MAINLAND_TZ)
        if local_now.weekday() >= 5:
            return False
        if venue == "cn" and local_now.date() in cls._MAINLAND_HOLIDAYS:
            return False
        if venue == "hk":
            sessions = ((time(9, 30), time(12)), (time(13), time(16, 10)))
        else:
            sessions = ((time(9, 30), time(11, 30)), (time(13), time(15)))
        if not any(start <= local_now.time() <= end for start, end in sessions):
            return False
        return now - as_of > timedelta(minutes=15)

    @classmethod
    def _is_recent_mainland_business_day(cls, source_date, current_date):
        if not cls._is_mainland_business_day(source_date):
            return False
        while not cls._is_mainland_business_day(current_date):
            current_date -= timedelta(days=1)
        previous = current_date - timedelta(days=1)
        while not cls._is_mainland_business_day(previous):
            previous -= timedelta(days=1)
        return source_date in {current_date, previous}

    @classmethod
    def _is_mainland_business_day(cls, value):
        if value.weekday() >= 5:
            return False
        if value.year == 2026:
            return value not in cls._MAINLAND_HOLIDAYS
        return True

    def _failed(self, code, provider, exc):
        return Valuation(
            code,
            Decimal("0"),
            Decimal("0"),
            self._now(),
            self._provider_name(provider),
            "failed",
            str(exc),
        )

    def _provider_for(self, holding):
        if self._is_hstech(holding.code):
            return self.hk_provider
        if holding.valuation_mode == "exchange":
            return self.exchange_collector
        if holding.valuation_mode in {"fund_nav", "qdii_nav"}:
            return self.nav_provider
        return self

    def _history_provider_for(self, target):
        if self._is_hstech(target.code):
            return self.hk_provider
        if target.valuation_mode == "exchange":
            return self.exchange_collector
        if target.valuation_mode in {"fund_nav", "qdii_nav"}:
            return self.nav_provider
        raise ValueError(f"unsupported valuation mode: {target.valuation_mode}")

    def _now(self):
        value = self.clock()
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    @staticmethod
    def _provider_name(provider):
        return type(provider).__name__

    @staticmethod
    def _decimal(value, field):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"{field} must be numeric") from None
        if not result.is_finite():
            raise ValueError(f"{field} must be finite")
        return result

    @staticmethod
    def _parse_timestamp(value):
        text = str(value or "").strip()
        if not text:
            raise ValueError("timestamp is required")
        if len(text) == 8 and text.isdigit():
            parsed = datetime.strptime(text, "%Y%m%d")
        elif len(text) == 10:
            parsed = datetime.combine(date.fromisoformat(text), time())
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=PortfolioValuationRouter.MAINLAND_TZ)

    @classmethod
    def _canonical_code(cls, code):
        text = str(code or "").strip().upper()
        return "HK.HSTECH" if text in cls.HSTECH_ALIASES else text

    @classmethod
    def _is_hstech(cls, code):
        return cls._canonical_code(code) == "HK.HSTECH"

    @classmethod
    def _target_for(cls, holding):
        code = cls._canonical_code(holding.code)
        if code == "HK.HSTECH":
            return _ValuationTarget(code, "exchange", "hk")
        venue = "hk" if str(getattr(holding, "market", "")).upper() == "HK" else "cn"
        return _ValuationTarget(code, holding.valuation_mode, venue)
