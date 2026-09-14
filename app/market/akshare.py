from datetime import datetime, timezone
import importlib
import math
import time

from app.chan.models import KLine


class AkShareProvider:
    """行情适配器，使用 AkShare 文档化的快照接口。"""

    _SNAPSHOT_FUNCTIONS = {
        "a": "stock_zh_a_spot_em",
        "etf": "fund_etf_spot_em",
    }
    _HISTORY_FUNCTIONS = {
        "a": {"daily": "stock_zh_a_hist", "minute": "stock_zh_a_hist_min_em"},
        "etf": {"daily": "fund_etf_hist_em"},
    }
    _ETF_PREFIXES = ("15", "16", "50", "51", "56", "58")
    _FIELD_ALIASES = {
        "code": ("代码", "code", "symbol", "ts_code"),
        "name": ("名称", "name", "简称", "csname"),
        "price": ("最新价", "最新", "收盘", "price", "close"),
        "change": ("涨跌幅", "涨跌幅(%)", "change", "pct_chg", "涨跌额"),
        "time": ("日期", "时间", "date", "day", "datetime", "trade_date"),
        "open": ("开盘", "open"),
        "high": ("最高", "high"),
        "low": ("最低", "low"),
        "close": ("收盘", "close"),
        "volume": ("成交量", "volume", "vol"),
    }

    def __init__(self, ak_module=None, clock=None, snapshot_ttl=15):
        self._ak = ak_module
        self._clock = clock or time.time
        self._snapshot_ttl = snapshot_ttl
        self._snapshots = {}

    def fetch(self, code: str):
        requested_code = str(code or "").strip()
        normalized_code, market = self._parse_code(requested_code)
        if not normalized_code:
            return None

        rows = self._get_snapshot(market)
        for row in rows:
            row_code = self._normalise_symbol(self._first(row, "code"), market)
            if row_code != normalized_code:
                continue

            try:
                price = float(self._first(row, "price"))
                change = float(self._first(row, "change"))
            except (TypeError, ValueError):
                return None
            if not math.isfinite(price) or not math.isfinite(change):
                return None

            return {
                "code": requested_code,
                "name": str(self._first(row, "name") or requested_code),
                "price": price,
                "change": change,
                "timestamp": datetime.fromtimestamp(
                    self._clock(), tz=timezone.utc
                ).isoformat(),
            }
        return None

    def fetch_klines(
        self,
        code,
        start_date=None,
        end_date=None,
        period="daily",
        adjust="",
    ):
        normalized_code, market = self._parse_code(str(code or "").strip())
        if not normalized_code:
            return []

        if period in ("daily", "weekly", "monthly"):
            function_name = self._HISTORY_FUNCTIONS[market]["daily"]
            start = start_date or "19700101"
            end = end_date or "22220101"
        elif period in ("1", "5", "15", "30", "60"):
            function_name = self._HISTORY_FUNCTIONS[market].get("minute")
            if function_name is None:
                raise ValueError("ETF minute history is not supported by AkShare adapter")
            start = start_date or "1979-09-01 09:30:00"
            end = end_date or "2222-01-01 15:00:00"
        else:
            raise ValueError("period must be daily, weekly, monthly, or a supported minute period")

        ak = self._ak or importlib.import_module("akshare")
        function = getattr(ak, function_name)
        frame = function(
            symbol=normalized_code,
            period=period,
            start_date=start,
            end_date=end,
            adjust=adjust,
        )
        rows = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else []
        lines = []
        for row in rows:
            try:
                values = [
                    float(self._first(row, field))
                    for field in ("open", "high", "low", "close", "volume")
                ]
                if not all(math.isfinite(value) for value in values):
                    raise ValueError
                if values[1] < values[2]:
                    raise ValueError
                timestamp = str(self._first(row, "time"))
                if not timestamp or timestamp == "None":
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ValueError("AkShare returned an invalid K-line row") from exc
            lines.append(KLine(timestamp, *values[:4], values[4]))
        return sorted(lines, key=lambda line: line.time)

    def _get_snapshot(self, market):
        current_time = self._clock()
        cached = self._snapshots.get(market)
        if cached is not None and current_time - cached["time"] < self._snapshot_ttl:
            return cached["rows"]

        ak = self._ak or importlib.import_module("akshare")
        function = getattr(ak, self._SNAPSHOT_FUNCTIONS[market])
        frame = function()
        rows = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else []
        self._snapshots[market] = {"time": current_time, "rows": rows}
        return rows

    @classmethod
    def _parse_code(cls, code):
        upper = code.upper()
        if upper.startswith("HK.") or upper.endswith(".HK") or (
            upper.isdigit() and len(upper) == 5
        ):
            raise ValueError("Hong Kong stock codes are not supported yet")
        if upper.startswith("SH.") or upper.startswith("SZ."):
            raw = upper[3:]
            market = "etf" if raw.startswith(cls._ETF_PREFIXES) else "a"
            return cls._normalise_symbol(raw, market), market
        market = "etf" if upper.startswith(cls._ETF_PREFIXES) else "a"
        return cls._normalise_symbol(upper, market), market

    @staticmethod
    def _normalise_symbol(value, market):
        if value is None:
            return ""
        text = str(value).strip().upper()
        if text.endswith(".SH") or text.endswith(".SZ"):
            text = text[:-3]
        if text.startswith("SH.") or text.startswith("SZ."):
            text = text[3:]
        if text.endswith(".0") and text[:-2].isdigit():
            text = text[:-2]
        width = 5 if market == "hk" else 6
        return text.zfill(width) if text.isdigit() else text

    @classmethod
    def _first(cls, row, field):
        for alias in cls._FIELD_ALIASES[field]:
            if alias in row:
                return row[alias]
        return None
