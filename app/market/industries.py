from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import importlib
import math
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from app.analysis.industry import (
    IndustryObservation,
    _validate_code,
    _validate_name,
)

SOURCE_AKSHARE_EASTMONEY_BOARD = "akshare_eastmoney_board"

ERROR_NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
ERROR_MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
ERROR_NO_USABLE_DATA = "NO_USABLE_DATA"
ERROR_DATE_MISMATCH = "DATE_MISMATCH"

VALID_ERROR_CODES = frozenset({
    ERROR_NETWORK_UNAVAILABLE,
    ERROR_MALFORMED_RESPONSE,
    ERROR_NO_USABLE_DATA,
    ERROR_DATE_MISMATCH,
})

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

ALIASES: dict[str, tuple[str, ...]] = {
    "code": ("板块代码", "代码", "code", "symbol", "secucode"),
    "name": ("板块名称", "名称", "name"),
    "return_1d_pct": ("涨跌幅", "涨跌幅(%)", "return_1d_pct", "change", "pct_chg"),
    "turnover_rate": ("换手率", "换手率(%)", "turnover_rate", "turnover"),
    "advancers": ("上涨家数", "上涨", "advancers", "up"),
    "decliners": ("下跌家数", "下跌", "decliners", "down"),
    "time": ("日期", "时间", "date", "datetime", "time"),
    "close": ("收盘", "收盘价", "close", "price", "最新价"),
}


def _classify_exception(exc: BaseException) -> str:
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return ERROR_NETWORK_UNAVAILABLE
    name = exc.__class__.__name__.lower()
    if any(k in name for k in ("connection", "timeout", "network", "urlerror", "httperror")):
        return ERROR_NETWORK_UNAVAILABLE
    try:
        import requests
        if isinstance(exc, requests.RequestException):
            return ERROR_NETWORK_UNAVAILABLE
    except ImportError:
        pass
    return ERROR_MALFORMED_RESPONSE


def _parse_float(val: Any, field_name: str) -> float:
    if type(val) is bool:
        raise ValueError(f"{field_name} cannot be a boolean")
    if val is None:
        raise ValueError(f"{field_name} cannot be None")
    try:
        f = float(val)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field_name} must be numeric") from None
    if not math.isfinite(f):
        raise ValueError(f"{field_name} must be finite")
    return f


def _parse_strict_or_integer_float(val: Any, field_name: str) -> int:
    if type(val) is bool:
        raise ValueError(f"{field_name} cannot be a boolean")
    if val is None:
        raise ValueError(f"{field_name} cannot be None")
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError(f"{field_name} must be finite")
        if not val.is_integer():
            raise ValueError(f"{field_name} float must be an integer value")
        return int(val)
    if isinstance(val, Decimal):
        if not val.is_finite():
            raise ValueError(f"{field_name} must be finite")
        if val % 1 != 0:
            raise ValueError(f"{field_name} decimal must be an integer value")
        return int(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            raise ValueError(f"{field_name} cannot be empty")
        try:
            f = float(s)
            if not math.isfinite(f):
                raise ValueError(f"{field_name} must be finite")
            if not f.is_integer():
                raise ValueError(f"{field_name} must be an integer value")
            return int(f)
        except (ValueError, OverflowError):
            raise ValueError(f"{field_name} is not a valid integer") from None
    raise ValueError(f"Unsupported type for {field_name}")


def _parse_date(val: Any, field_name: str) -> date:
    if type(val) is bool:
        raise ValueError(f"{field_name} cannot be a boolean")
    if val is None:
        raise ValueError(f"{field_name} cannot be None")
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s or len(s) > 256:
            raise ValueError(f"{field_name} cannot be empty or too long")
        token = s.split(" ")[0].split("T")[0].replace("/", "-")
        if len(token) == 8 and token.isdigit():
            try:
                return datetime.strptime(token, "%Y%m%d").date()
            except ValueError:
                raise ValueError(f"{field_name} invalid date format") from None
        try:
            return date.fromisoformat(token)
        except ValueError:
            raise ValueError(f"{field_name} invalid date format") from None
    if hasattr(val, "date") and callable(getattr(val, "date")):
        return val.date()
    raise ValueError(f"Unsupported date type for {field_name}")


@dataclass(frozen=True)
class IndustryFetch:
    observations: tuple[IndustryObservation, ...]
    errors: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    truncated: bool = False

    def __post_init__(self) -> None:
        if type(self.observations) is not tuple:
            raise TypeError("observations must be a tuple")
        for obs in self.observations:
            if not isinstance(obs, IndustryObservation):
                raise TypeError("each item in observations must be an IndustryObservation instance")

        seen_codes: set[str] = set()
        for obs in self.observations:
            if obs.code in seen_codes:
                raise ValueError(f"duplicate code found in observations: {obs.code}")
            seen_codes.add(obs.code)

        if not isinstance(self.errors, Mapping):
            raise TypeError("errors must be a Mapping")

        copied_errors = dict(self.errors)
        for k, v in copied_errors.items():
            if type(k) is not str or not k.strip():
                raise ValueError("errors keys must be non-empty strings")
            if v not in VALID_ERROR_CODES:
                raise ValueError(f"errors values must be one of the recognized error codes: {v}")

        if seen_codes.intersection(copied_errors.keys()):
            raise ValueError("observations and errors must be mutually exclusive")

        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a boolean")

        if self.observations:
            first_as_of = self.observations[0].as_of
            for obs in self.observations:
                if obs.source != SOURCE_AKSHARE_EASTMONEY_BOARD:
                    raise ValueError(f"all observations must have source {SOURCE_AKSHARE_EASTMONEY_BOARD}")
                if obs.as_of != first_as_of:
                    raise ValueError("all observations must share the same as_of")

        object.__setattr__(self, "errors", MappingProxyType(copied_errors))


class AkShareIndustryProvider:
    SOURCE = SOURCE_AKSHARE_EASTMONEY_BOARD

    def __init__(
        self,
        client: Any = None,
        clock: Any = None,
        max_boards: int | None = None,
        history_days: int = 40,
    ) -> None:
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if max_boards is not None:
            if type(max_boards) is bool:
                raise ValueError("max_boards cannot be a boolean")
            if not isinstance(max_boards, int):
                raise TypeError("max_boards must be an integer or None")
            if max_boards < 1:
                raise ValueError("max_boards must be at least 1")
        if type(history_days) is bool:
            raise ValueError("history_days cannot be a boolean")
        if not isinstance(history_days, int):
            raise TypeError("history_days must be an integer")
        if history_days < 1:
            raise ValueError("history_days must be at least 1")

        self._client = client
        self._clock = clock
        self._max_boards = max_boards
        self._history_days = history_days

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        return importlib.import_module("akshare")

    def _get_fetched_at(self) -> datetime:
        if self._clock is not None:
            now = self._clock()
            if type(now) is bool or not isinstance(now, datetime):
                raise TypeError(f"clock must return a datetime instance, got {type(now).__name__}")
            if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
                raise ValueError("clock must return a timezone-aware datetime")
            return now
        return datetime.now(timezone.utc)

    @staticmethod
    def _first(row: Mapping[str, Any], field_name: str) -> Any:
        for alias in ALIASES.get(field_name, ()):
            if alias in row:
                return row[alias]
        return None

    @classmethod
    def _get_code_key(cls, row: Any) -> str:
        if not isinstance(row, Mapping):
            return ""
        val = cls._first(row, "code")
        return str(val or "").strip()

    def fetch(self, cutoff: datetime, market_date: date) -> IndustryFetch:
        if isinstance(market_date, datetime) or not isinstance(market_date, date) or type(market_date) is bool:
            raise TypeError("market_date must be datetime.date and not datetime")
        if not isinstance(cutoff, datetime):
            raise TypeError("cutoff must be a datetime instance")
        if cutoff.tzinfo is None or cutoff.tzinfo.utcoffset(cutoff) is None:
            raise ValueError("cutoff must be timezone-aware")

        fetched_at = self._get_fetched_at()

        try:
            client = self._get_client()
            getter = getattr(client, "stock_board_industry_name_em", None)
            if getter is None:
                return IndustryFetch(observations=(), errors={}, truncated=False)
            frame = getter()
            rows = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else []
            if not isinstance(rows, list):
                return IndustryFetch(observations=(), errors={}, truncated=False)
        except Exception:
            return IndustryFetch(observations=(), errors={}, truncated=False)

        if not rows:
            return IndustryFetch(observations=(), errors={}, truncated=False)

        sorted_rows = sorted(rows, key=self._get_code_key)
        if self._max_boards is not None and len(sorted_rows) > self._max_boards:
            selected_rows = sorted_rows[: self._max_boards]
            truncated = True
        else:
            selected_rows = sorted_rows
            truncated = False

        as_of = datetime.combine(market_date, time(15, 0), tzinfo=SHANGHAI_TZ)

        observations: list[IndustryObservation] = []
        errors: dict[str, str] = {}
        seen_codes: set[str] = set()

        for row in selected_rows:
            if not isinstance(row, Mapping):
                continue
            raw_code = self._first(row, "code")
            if raw_code is None:
                continue
            raw_code_str = str(raw_code).strip()
            if not raw_code_str:
                continue

            try:
                code = _validate_code(raw_code)
            except (TypeError, ValueError):
                errors[raw_code_str] = ERROR_MALFORMED_RESPONSE
                continue

            if code in seen_codes:
                observations = [o for o in observations if o.code != code]
                errors[code] = ERROR_MALFORMED_RESPONSE
                continue
            seen_codes.add(code)

            raw_name = self._first(row, "name")
            try:
                name = _validate_name(raw_name)
            except (TypeError, ValueError):
                errors[code] = ERROR_MALFORMED_RESPONSE
                continue

            raw_ret1d = self._first(row, "return_1d_pct")
            try:
                ret_1d = _parse_float(raw_ret1d, "return_1d_pct")
            except (TypeError, ValueError):
                errors[code] = ERROR_MALFORMED_RESPONSE
                continue

            raw_to = self._first(row, "turnover_rate")
            try:
                turnover_rate = _parse_float(raw_to, "turnover_rate")
                if turnover_rate < 0:
                    raise ValueError("turnover_rate cannot be negative")
            except (TypeError, ValueError):
                errors[code] = ERROR_MALFORMED_RESPONSE
                continue

            raw_adv = self._first(row, "advancers")
            raw_dec = self._first(row, "decliners")
            try:
                advancers = _parse_strict_or_integer_float(raw_adv, "advancers")
                decliners = _parse_strict_or_integer_float(raw_dec, "decliners")
                if advancers < 0 or decliners < 0:
                    raise ValueError("advancers and decliners must be non-negative")
                if advancers + decliners <= 0:
                    raise ValueError("sum of advancers and decliners must be > 0")
            except (TypeError, ValueError):
                errors[code] = ERROR_MALFORMED_RESPONSE
                continue

            if as_of > fetched_at:
                errors[code] = ERROR_DATE_MISMATCH
                continue

            start_date_str = (market_date - timedelta(days=self._history_days)).strftime("%Y%m%d")
            end_date_str = (market_date + timedelta(days=self._history_days)).strftime("%Y%m%d")
            try:
                hist_getter = getattr(client, "stock_board_industry_hist_em", None)
                if hist_getter is None:
                    errors[code] = ERROR_MALFORMED_RESPONSE
                    continue
                df_hist = hist_getter(
                    symbol=code,
                    start_date=start_date_str,
                    end_date=end_date_str,
                    period="日k",
                    adjust="",
                )
            except (Exception, MemoryError) as exc:
                errors[code] = _classify_exception(exc)
                continue

            if hasattr(df_hist, "to_dict"):
                k_rows = df_hist.to_dict(orient="records")
            elif isinstance(df_hist, list):
                k_rows = df_hist
            else:
                errors[code] = ERROR_MALFORMED_RESPONSE
                continue

            if not k_rows:
                errors[code] = ERROR_NO_USABLE_DATA
                continue

            parsed_klines: list[tuple[date, float]] = []
            k_malformed = False
            for k_row in k_rows:
                if not isinstance(k_row, Mapping):
                    k_malformed = True
                    break
                r_date = self._first(k_row, "time")
                r_close = self._first(k_row, "close")
                try:
                    kd = _parse_date(r_date, "日期")
                    kc = _parse_float(r_close, "收盘")
                except (TypeError, ValueError):
                    k_malformed = True
                    break
                parsed_klines.append((kd, kc))

            if k_malformed:
                errors[code] = ERROR_MALFORMED_RESPONSE
                continue

            parsed_klines.sort(key=lambda item: item[0])

            if parsed_klines[-1][0] != market_date:
                errors[code] = ERROR_DATE_MISMATCH
                continue

            if len(parsed_klines) < 21:
                errors[code] = ERROR_NO_USABLE_DATA
                continue

            close_last = parsed_klines[-1][1]
            close_5d = parsed_klines[-6][1]
            close_20d = parsed_klines[-21][1]

            if close_last <= 0 or close_5d <= 0 or close_20d <= 0:
                errors[code] = ERROR_NO_USABLE_DATA
                continue

            ret_5d_pct = (close_last / close_5d - 1.0) * 100.0
            ret_20d_pct = (close_last / close_20d - 1.0) * 100.0

            if not (math.isfinite(ret_5d_pct) and math.isfinite(ret_20d_pct)):
                errors[code] = ERROR_MALFORMED_RESPONSE
                continue

            try:
                obs = IndustryObservation(
                    code=code,
                    name=name,
                    as_of=as_of,
                    fetched_at=fetched_at,
                    source=self.SOURCE,
                    return_1d_pct=ret_1d,
                    return_5d_pct=ret_5d_pct,
                    return_20d_pct=ret_20d_pct,
                    turnover_rate=turnover_rate,
                    advancers=advancers,
                    decliners=decliners,
                )
                observations.append(obs)
            except (TypeError, ValueError):
                errors[code] = ERROR_MALFORMED_RESPONSE
                continue

        return IndustryFetch(
            observations=tuple(observations),
            errors=errors,
            truncated=truncated,
        )


__all__ = [
    "SOURCE_AKSHARE_EASTMONEY_BOARD",
    "ERROR_NETWORK_UNAVAILABLE",
    "ERROR_MALFORMED_RESPONSE",
    "ERROR_NO_USABLE_DATA",
    "ERROR_DATE_MISMATCH",
    "VALID_ERROR_CODES",
    "IndustryFetch",
    "AkShareIndustryProvider",
]
