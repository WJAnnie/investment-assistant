"""AkShare adapter for public-fund net asset values."""

from datetime import date, datetime
import importlib
import math

from app.chan.models import KLine

from .models import Quote


class AkShareFundNavProvider:
    """Fetch fund NAV history without importing AkShare at module import time."""

    FUNCTION_NAME = "fund_open_fund_info_em"
    INDICATOR = "单位净值走势"

    def __init__(self, ak_module=None):
        self._ak = ak_module

    def fetch(self, code: str):
        code = self._validate_code(code)
        rows = self._fetch_rows(code)
        ordered = self._strict_nav_rows(rows)
        if not ordered:
            return None

        nav_date, row, _nav = ordered[-1]
        _nav_date, nav, change = self._parse_nav_row(row, include_change=True)
        date_text = nav_date.isoformat()
        return Quote(
            code,
            code,
            nav,
            change,
            date_text,
            source="akshare_fund_nav",
            market_time=date_text,
        )

    def fetch_klines(self, code: str, **_kwargs):
        code = self._validate_code(code)
        rows = self._fetch_rows(code)
        parsed = self._strict_nav_rows(rows)
        if not parsed:
            return []
        return [
            KLine(nav_date.isoformat(), nav, nav, nav, nav, 0.0)
            for nav_date, _row, nav in parsed
        ]

    def _fetch_rows(self, code):
        ak = self._ak or importlib.import_module("akshare")
        frame = getattr(ak, self.FUNCTION_NAME)(
            symbol=code,
            indicator=self.INDICATOR,
        )
        return frame.to_dict(orient="records") if hasattr(frame, "to_dict") else []

    @staticmethod
    def _validate_code(code):
        text = str(code or "").strip()
        if len(text) != 6 or not text.isdigit():
            raise ValueError("fund code must be a six-digit code")
        return text

    @classmethod
    def _strict_nav_rows(cls, rows):
        parsed = []
        for row in rows:
            try:
                nav_date = cls._parse_date(row.get("净值日期"))
                nav = cls._number(row.get("单位净值"), "NAV")
                if nav <= 0:
                    raise ValueError("NAV must be finite and positive")
            except (AttributeError, TypeError, ValueError, OverflowError):
                raise ValueError("AkShare returned an invalid NAV history row") from None
            parsed.append((nav_date, row, nav))
        return sorted(parsed, key=lambda item: item[0])

    @classmethod
    def _parse_nav_row(cls, row, include_change):
        try:
            nav_date = cls._parse_date(row.get("净值日期"))
            nav = cls._number(row.get("单位净值"), "NAV")
            if nav <= 0:
                raise ValueError("NAV must be finite and positive")
            change = 0.0
            if include_change:
                change = cls._number(row.get("日增长率"), "NAV change")
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("AkShare returned an invalid latest NAV row") from exc
        return nav_date, nav, change

    @staticmethod
    def _number(value, field):
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field} must be numeric") from exc
        if not math.isfinite(result):
            raise ValueError(f"{field} must be finite")
        return result

    @staticmethod
    def _parse_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value or "").strip()
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return date.fromisoformat(text)
        raise ValueError("NAV date must be YYYY-MM-DD or YYYYMMDD")
