from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
import math
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from app.analysis.fundamental import (
    StockFundamentalObservation,
    StockValuationObservation,
)

ERROR_NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
ERROR_MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
ERROR_SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
ERROR_NO_USABLE_FUNDAMENTAL = "NO_USABLE_FUNDAMENTAL"
ERROR_NO_USABLE_VALUATION = "NO_USABLE_VALUATION"

VALID_ERROR_KEYS = frozenset({"fundamental", "valuation"})
VALID_ERROR_CODES = frozenset({
    ERROR_NETWORK_UNAVAILABLE,
    ERROR_MALFORMED_RESPONSE,
    ERROR_SYMBOL_MISMATCH,
    ERROR_NO_USABLE_FUNDAMENTAL,
    ERROR_NO_USABLE_VALUATION,
})

SOURCE_AKSHARE_EASTMONEY = "akshare_eastmoney"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MAX_FIELD_STRING_LENGTH = 256

REQUIRED_FUNDAMENTAL_COLUMNS = (
    "SECUCODE",
    "SECURITY_CODE",
    "REPORT_DATE",
    "NOTICE_DATE",
    "TOTALOPERATEREVETZ",
    "PARENTNETPROFITTZ",
    "ROEJQ",
    "EPSJB",
)

REQUIRED_VALUATION_COLUMNS = (
    "数据日期",
    "PE(TTM)",
    "市净率",
    "市销率",
)


@dataclass(frozen=True)
class StockEvidenceFetch:
    symbol: str
    fundamental: StockFundamentalObservation | None
    valuation: StockValuationObservation | None
    errors: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if type(self.symbol) is not str:
            raise TypeError("symbol must be a string")
        if len(self.symbol) != 6 or not self.symbol.isascii() or not self.symbol.isdigit():
            raise ValueError("symbol must be a 6-digit A-share code")

        if self.fundamental is not None:
            if not isinstance(self.fundamental, StockFundamentalObservation):
                raise TypeError("fundamental must be a StockFundamentalObservation instance or None")
            if self.fundamental.symbol != self.symbol:
                raise ValueError("fundamental symbol mismatch")

        if self.valuation is not None:
            if not isinstance(self.valuation, StockValuationObservation):
                raise TypeError("valuation must be a StockValuationObservation instance or None")
            if self.valuation.symbol != self.symbol:
                raise ValueError("valuation symbol mismatch")

        if not isinstance(self.errors, Mapping):
            raise TypeError("errors must be a Mapping")

        copied_errors = dict(self.errors)
        for k, v in copied_errors.items():
            if k not in VALID_ERROR_KEYS:
                raise ValueError("errors keys must be 'fundamental' or 'valuation'")
            if v not in VALID_ERROR_CODES:
                raise ValueError("errors values must be one of the recognized error codes")

        if self.fundamental is not None:
            if "fundamental" in copied_errors:
                raise ValueError("endpoint cannot have both observation and error")
        else:
            if "fundamental" not in copied_errors:
                raise ValueError("missing fundamental observation must have error")

        if self.valuation is not None:
            if "valuation" in copied_errors:
                raise ValueError("endpoint cannot have both observation and error")
        else:
            if "valuation" not in copied_errors:
                raise ValueError("missing valuation observation must have error")

        object.__setattr__(self, "errors", MappingProxyType(copied_errors))


def _validate_symbol(symbol: Any) -> tuple[str, str]:
    if type(symbol) is not str:
        raise TypeError("symbol must be a string")
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("symbol must be a 6-digit A-share code")
    if symbol.startswith("6"):
        mapped_symbol = f"{symbol}.SH"
    elif symbol.startswith(("0", "3")):
        mapped_symbol = f"{symbol}.SZ"
    elif symbol.startswith(("4", "8", "92")):
        mapped_symbol = f"{symbol}.BJ"
    else:
        raise ValueError("symbol is not a supported A-share code")
    return symbol, mapped_symbol


def _validate_cutoff(cutoff: Any) -> datetime:
    if not isinstance(cutoff, datetime):
        raise TypeError("cutoff must be a datetime instance")
    if cutoff.tzinfo is None or cutoff.tzinfo.utcoffset(cutoff) is None:
        raise ValueError("cutoff must be timezone-aware")
    return cutoff


def _parse_numeric(val: Any, field_name: str) -> float | Decimal | int:
    if type(val) is bool:
        raise ValueError(f"{field_name} cannot be a boolean")
    if val is None or pd.isna(val):
        raise ValueError(f"{field_name} cannot be null or NaN")
    if isinstance(val, (int, float, Decimal)):
        if isinstance(val, float) and not math.isfinite(val):
            raise ValueError(f"{field_name} must be finite")
        if isinstance(val, Decimal) and not val.is_finite():
            raise ValueError(f"{field_name} must be finite")
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s or len(s) > MAX_FIELD_STRING_LENGTH:
            raise ValueError(f"{field_name} cannot be empty or too long")
        try:
            d = Decimal(s)
        except Exception:
            raise ValueError(f"{field_name} is not a valid number") from None
        if not d.is_finite():
            raise ValueError(f"{field_name} must be finite")
        return d
    raise ValueError(f"Unsupported number type for {field_name}")


def _parse_date(val: Any, field_name: str) -> date:
    if type(val) is bool:
        raise ValueError(f"{field_name} cannot be a boolean")
    if val is None or pd.isna(val):
        raise ValueError(f"{field_name} cannot be null or NaN")
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s or len(s) > MAX_FIELD_STRING_LENGTH:
            raise ValueError(f"{field_name} cannot be empty or too long")
        token = s.split(" ")[0].split("T")[0].replace("/", "-")
        try:
            return date.fromisoformat(token)
        except ValueError:
            raise ValueError(f"{field_name} invalid date format") from None
    raise ValueError(f"Unsupported date type for {field_name}")


def _parse_shanghai_datetime(val: Any, field_name: str) -> datetime:
    if type(val) is bool:
        raise ValueError(f"{field_name} cannot be a boolean")
    if val is None or pd.isna(val):
        raise ValueError(f"{field_name} cannot be null or NaN")
    if isinstance(val, pd.Timestamp):
        val = val.to_pydatetime()
    if isinstance(val, datetime):
        if val.tzinfo is None or val.tzinfo.utcoffset(val) is None:
            return val.replace(tzinfo=SHANGHAI_TZ)
        return val.astimezone(SHANGHAI_TZ)
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day, 0, 0, 0, tzinfo=SHANGHAI_TZ)
    if isinstance(val, str):
        s = val.strip()
        if not s or len(s) > MAX_FIELD_STRING_LENGTH:
            raise ValueError(f"{field_name} cannot be empty or too long")
        clean_s = s.replace("/", "-")
        try:
            dt = datetime.fromisoformat(clean_s)
        except ValueError:
            try:
                d = date.fromisoformat(clean_s.split(" ")[0].split("T")[0])
                dt = datetime(d.year, d.month, d.day, 0, 0, 0)
            except ValueError:
                raise ValueError(f"{field_name} invalid datetime format") from None
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            return dt.replace(tzinfo=SHANGHAI_TZ)
        return dt.astimezone(SHANGHAI_TZ)
    raise ValueError(f"Unsupported datetime type for {field_name}")


def _parse_str(val: Any, field_name: str) -> str:
    if type(val) is bool:
        raise ValueError(f"{field_name} cannot be a boolean")
    if val is None or pd.isna(val):
        raise ValueError(f"{field_name} cannot be null or NaN")
    s = str(val).strip()
    if not s:
        raise ValueError(f"{field_name} cannot be empty")
    if len(s) > MAX_FIELD_STRING_LENGTH:
        raise ValueError(f"{field_name} exceeds max string length")
    return s


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


def _process_fundamental_df(
    df: Any,
    symbol: str,
    mapped_symbol: str,
    cutoff: datetime,
    fetched_at: datetime,
    max_rows: int,
) -> tuple[StockFundamentalObservation | None, str | None]:
    if not isinstance(df, pd.DataFrame):
        return None, ERROR_MALFORMED_RESPONSE

    # Duplicate columns check
    if len(df.columns) != len(set(df.columns)):
        return None, ERROR_MALFORMED_RESPONSE

    # Row count check
    if len(df) < 1 or len(df) > max_rows:
        return None, ERROR_MALFORMED_RESPONSE

    # Required columns check
    for col in REQUIRED_FUNDAMENTAL_COLUMNS:
        if col not in df.columns:
            return None, ERROR_MALFORMED_RESPONSE

    # Validate symbol and dates for every row (do not parse metrics here)
    parsed_rows: list[tuple[datetime, date, int]] = []
    for idx in range(len(df)):
        row = df.iloc[idx]
        try:
            secucode = _parse_str(row["SECUCODE"], "SECUCODE")
            sec_code = _parse_str(row["SECURITY_CODE"], "SECURITY_CODE")
            rep_date = _parse_date(row["REPORT_DATE"], "REPORT_DATE")
            notice_dt = _parse_shanghai_datetime(row["NOTICE_DATE"], "NOTICE_DATE")
        except (ValueError, TypeError):
            return None, ERROR_MALFORMED_RESPONSE

        # Symbol check
        if sec_code != symbol or not (secucode == mapped_symbol or secucode.startswith(symbol)):
            return None, ERROR_SYMBOL_MISMATCH

        parsed_rows.append((notice_dt, rep_date, idx))

    # Filter cutoff
    valid_rows = [r for r in parsed_rows if r[0] <= cutoff]
    if not valid_rows:
        return None, ERROR_NO_USABLE_FUNDAMENTAL

    # Select latest announcement (order-independent)
    valid_rows.sort(key=lambda c: (c[0], c[1]), reverse=True)
    latest_notice_dt, latest_rep_date, latest_idx = valid_rows[0]

    # Only parse metrics on the selected latest row
    selected_row = df.iloc[latest_idx]
    try:
        rev_growth = _parse_numeric(selected_row["TOTALOPERATEREVETZ"], "TOTALOPERATEREVETZ")
        profit_growth = _parse_numeric(selected_row["PARENTNETPROFITTZ"], "PARENTNETPROFITTZ")
        roe = _parse_numeric(selected_row["ROEJQ"], "ROEJQ")
        eps = _parse_numeric(selected_row["EPSJB"], "EPSJB")
    except (ValueError, TypeError):
        return None, ERROR_MALFORMED_RESPONSE

    try:
        obs = StockFundamentalObservation(
            symbol=symbol,
            report_period=latest_rep_date,
            published_at=latest_notice_dt,
            fetched_at=fetched_at,
            source=SOURCE_AKSHARE_EASTMONEY,
            revenue_growth_pct=rev_growth,
            net_profit_growth_pct=profit_growth,
            roe_pct=roe,
            eps=eps,
        )
        return obs, None
    except (ValueError, TypeError):
        return None, ERROR_MALFORMED_RESPONSE


def _process_valuation_df(
    df: Any,
    symbol: str,
    cutoff: datetime,
    fetched_at: datetime,
    max_rows: int,
) -> tuple[StockValuationObservation | None, str | None]:
    if not isinstance(df, pd.DataFrame):
        return None, ERROR_MALFORMED_RESPONSE

    # Duplicate columns check
    if len(df.columns) != len(set(df.columns)):
        return None, ERROR_MALFORMED_RESPONSE

    # Row count check
    if len(df) < 1 or len(df) > max_rows:
        return None, ERROR_MALFORMED_RESPONSE

    # Required columns check
    for col in REQUIRED_VALUATION_COLUMNS:
        if col not in df.columns:
            return None, ERROR_MALFORMED_RESPONSE

    # Check symbol column for every row if present
    for sym_col in ("SECURITY_CODE", "代码", "symbol"):
        if sym_col in df.columns:
            for idx in range(len(df)):
                try:
                    row_sym = _parse_str(df.iloc[idx][sym_col], sym_col)
                except (ValueError, TypeError):
                    return None, ERROR_MALFORMED_RESPONSE
                if row_sym != symbol and not row_sym.startswith(symbol):
                    return None, ERROR_SYMBOL_MISMATCH

    # Validate dates for every row (do not parse metrics here)
    parsed_dates: list[tuple[datetime, int]] = []
    for idx in range(len(df)):
        row = df.iloc[idx]
        try:
            as_of_dt = _parse_shanghai_datetime(row["数据日期"], "数据日期")
        except (ValueError, TypeError):
            return None, ERROR_MALFORMED_RESPONSE
        parsed_dates.append((as_of_dt, idx))

    # Filter cutoff
    valid_rows = [r for r in parsed_dates if r[0] <= cutoff]
    if not valid_rows:
        return None, ERROR_NO_USABLE_VALUATION

    # Select latest row (order-independent)
    valid_rows.sort(key=lambda c: c[0], reverse=True)
    latest_as_of_dt, latest_idx = valid_rows[0]

    # Only parse metrics on the selected latest row
    selected_row = df.iloc[latest_idx]
    try:
        pe_ttm = _parse_numeric(selected_row["PE(TTM)"], "PE(TTM)")
        pb = _parse_numeric(selected_row["市净率"], "市净率")
        ps = _parse_numeric(selected_row["市销率"], "市销率")
    except (ValueError, TypeError):
        return None, ERROR_MALFORMED_RESPONSE

    try:
        obs = StockValuationObservation(
            symbol=symbol,
            as_of=latest_as_of_dt,
            fetched_at=fetched_at,
            source=SOURCE_AKSHARE_EASTMONEY,
            pe_ttm=pe_ttm,
            pb=pb,
            ps=ps,
        )
        return obs, None
    except (ValueError, TypeError):
        return None, ERROR_MALFORMED_RESPONSE


class AkShareStockEvidenceProvider:
    def __init__(
        self,
        client: Any = None,
        clock: Any = None,
        max_rows: int = 4096,
    ) -> None:
        if type(max_rows) is bool or not isinstance(max_rows, int):
            raise TypeError("max_rows must be an integer, not bool, float, or string")
        if max_rows < 1 or max_rows > 4096:
            raise ValueError("max_rows must be between 1 and 4096")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._client = client
        self._clock = clock
        self._max_rows = max_rows

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import akshare
        return akshare

    def _get_fetched_at(self) -> datetime:
        if self._clock is not None:
            now = self._clock()
            if type(now) is bool:
                raise TypeError("clock returned bool, timezone-aware datetime required")
            if not isinstance(now, datetime):
                raise TypeError(
                    f"clock returned {type(now).__name__}, timezone-aware datetime required"
                )
            if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
                raise ValueError("clock returned naive datetime, timezone-aware datetime required")
            return now
        return datetime.now(SHANGHAI_TZ)

    def fetch(self, symbol: str, cutoff: datetime) -> StockEvidenceFetch:
        valid_symbol, mapped_symbol = _validate_symbol(symbol)
        valid_cutoff = _validate_cutoff(cutoff)

        errors: dict[str, str] = {}
        fundamental_obs: StockFundamentalObservation | None = None
        valuation_obs: StockValuationObservation | None = None

        # 1. Fundamental endpoint call and failure isolation
        try:
            client = self._get_client()
            getter = getattr(client, "stock_financial_analysis_indicator_em")
            df_fund = getter(symbol=mapped_symbol, indicator="按报告期")
            fund_fetched_at = self._get_fetched_at()
            fundamental_obs, fund_err = _process_fundamental_df(
                df=df_fund,
                symbol=valid_symbol,
                mapped_symbol=mapped_symbol,
                cutoff=valid_cutoff,
                fetched_at=fund_fetched_at,
                max_rows=self._max_rows,
            )
            if fund_err:
                errors["fundamental"] = fund_err
        except (Exception, MemoryError) as exc:
            errors["fundamental"] = _classify_exception(exc)

        # 2. Valuation endpoint call and failure isolation
        try:
            client = self._get_client()
            getter = getattr(client, "stock_value_em")
            df_val = getter(symbol=valid_symbol)
            val_fetched_at = self._get_fetched_at()
            valuation_obs, val_err = _process_valuation_df(
                df=df_val,
                symbol=valid_symbol,
                cutoff=valid_cutoff,
                fetched_at=val_fetched_at,
                max_rows=self._max_rows,
            )
            if val_err:
                errors["valuation"] = val_err
        except (Exception, MemoryError) as exc:
            errors["valuation"] = _classify_exception(exc)

        return StockEvidenceFetch(
            symbol=valid_symbol,
            fundamental=fundamental_obs,
            valuation=valuation_obs,
            errors=errors,
        )
