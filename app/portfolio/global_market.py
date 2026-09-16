"""Offline pure-functional overnight global market computation module.

This module consumes injected observations, validates timestamps and numeric
ranges strictly against an explicit cutoff, calculates overnight price changes,
evaluates freshness, derives overnight summary conclusions without prediction,
and formats human-readable lines isolated from any internal diagnostics.
"""

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
import math
from types import MappingProxyType
from zoneinfo import ZoneInfo

from app.market.global_markets import (
    ERROR_CODES,
    GlobalMarketDataError,
    INSTRUMENTS,
    SourceObservation,
)


GLOBAL_SYMBOLS: tuple[str, ...] = tuple(INSTRUMENTS)

FRESHNESS_LABELS: Mapping[str, str] = MappingProxyType({
    "RECENT": "数据较新",
    "DELAYED_OR_HOLIDAY": "可能因延迟或休市未更新",
    "STALE": "数据已陈旧，请谨慎核对",
})

STATUS_LABELS: Mapping[str, str] = MappingProxyType({
    "complete": "完整性：8 项隔夜行情均已获取。",
    "partial": "完整性：部分隔夜行情暂不可用，其余项目照常展示。",
    "unavailable": "完整性：隔夜行情暂不可用，本次不作方向判断。",
    "not_collected": "完整性：本阶段不采集隔夜行情。",
})

SUMMARY_NOTE: str = "（仅为隔夜事实归纳，不构成任何投资建议）"

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_TREASURY_SYMBOL = "^TNX"
_YAHOO_SOURCE = "yahoo_chart"
_FRED_SOURCE = "fred_dgs10"

_QUOTE_KEYS = (
    "category",
    "symbol",
    "name",
    "value",
    "previous_value",
    "value_unit",
    "change",
    "change_unit",
    "source",
    "source_as_of",
    "session_date",
    "freshness",
    "error_code",
)


def _provider_error_code(exc: Exception) -> str:
    """Map provider exception to catalogue code or fallback constant."""
    if isinstance(exc, GlobalMarketDataError) and getattr(exc, "code", None) in ERROR_CODES:
        return exc.code
    return "PROVIDER_UNAVAILABLE"


def gather_global_observations(
    global_provider,
    treasury_fallback,
    symbols: tuple[str, ...] = GLOBAL_SYMBOLS,
) -> tuple[tuple, ...]:
    """Pull observations per symbol, failing closed without leaking exception details."""
    results = []
    for symbol in symbols:
        if symbol == _TREASURY_SYMBOL:
            # 1. Try global_provider first
            obs = None
            if global_provider is not None:
                try:
                    candidate = global_provider.fetch(symbol)
                    if isinstance(candidate, SourceObservation) and candidate.source == _FRED_SOURCE:
                        obs = candidate
                except Exception:
                    obs = None

            if obs is not None:
                results.append((symbol, obs, None, _FRED_SOURCE))
                continue

            # 2. Fall back to treasury_fallback
            fb_obs = None
            if treasury_fallback is not None:
                try:
                    candidate = treasury_fallback.fetch(symbol)
                    if isinstance(candidate, SourceObservation):
                        fb_obs = candidate
                except Exception:
                    fb_obs = None

            if fb_obs is not None:
                results.append((symbol, fb_obs, None, _FRED_SOURCE))
            else:
                results.append((symbol, None, "TREASURY_SOURCES_UNAVAILABLE", None))
        else:
            # Non-treasury symbols never call treasury_fallback
            if global_provider is None:
                results.append((symbol, None, "PROVIDER_UNAVAILABLE", None))
                continue
            try:
                candidate = global_provider.fetch(symbol)
                if isinstance(candidate, SourceObservation):
                    results.append((symbol, candidate, None, _YAHOO_SOURCE))
                else:
                    results.append((symbol, None, "PROVIDER_UNAVAILABLE", None))
            except Exception as exc:
                err_code = _provider_error_code(exc)
                results.append((symbol, None, err_code, None))

    return tuple(results)


def _failed_record(symbol: str, error_code: str) -> dict:
    """Return a failed quote record preserving key set and catalog metadata."""
    entry = INSTRUMENTS.get(symbol)
    return {
        "category": entry.category if entry else None,
        "symbol": symbol,
        "name": entry.name if entry else symbol,
        "value": None,
        "previous_value": None,
        "value_unit": entry.value_unit if entry else None,
        "change": None,
        "change_unit": None,
        "source": None,
        "source_as_of": None,
        "session_date": None,
        "freshness": None,
        "error_code": error_code,
    }


def _build_single_quote(item: tuple, cutoff: datetime) -> dict:
    """Build and strictly validate one quote record from observation item."""
    symbol = item[0]
    entry = INSTRUMENTS.get(symbol)
    if entry is None:
        return _failed_record(symbol, "INVALID_OBSERVATION")

    obs = item[1]
    err_code = item[2]
    if err_code is not None or obs is None or not isinstance(obs, SourceObservation):
        return _failed_record(symbol, err_code if err_code is not None else "INVALID_OBSERVATION")

    val = obs.value
    prev_val = obs.previous_value
    if (
        type(val) not in (int, float)
        or isinstance(val, bool)
        or not math.isfinite(val)
        or val <= 0
        or type(prev_val) not in (int, float)
        or isinstance(prev_val, bool)
        or not math.isfinite(prev_val)
        or prev_val <= 0
    ):
        return _failed_record(symbol, "INVALID_OBSERVATION")

    sd = obs.session_date
    if not isinstance(sd, str):
        return _failed_record(symbol, "INVALID_OBSERVATION")
    try:
        parsed_sd = date.fromisoformat(sd)
        if parsed_sd.isoformat() != sd:
            return _failed_record(symbol, "INVALID_OBSERVATION")
    except (ValueError, TypeError):
        return _failed_record(symbol, "INVALID_OBSERVATION")

    # Source and source_time resolution
    if obs.source == _YAHOO_SOURCE and symbol != _TREASURY_SYMBOL:
        if not isinstance(obs.source_as_of, str):
            return _failed_record(symbol, "INVALID_OBSERVATION")
        try:
            source_time = datetime.fromisoformat(obs.source_as_of)
            if source_time.tzinfo is None or source_time.tzinfo.utcoffset(source_time) is None:
                return _failed_record(symbol, "INVALID_OBSERVATION")
        except (ValueError, TypeError):
            return _failed_record(symbol, "INVALID_OBSERVATION")
    elif symbol == _TREASURY_SYMBOL and obs.source == _FRED_SOURCE and obs.source_as_of is None:
        source_time = datetime.combine(parsed_sd, time(23, 59, 59), tzinfo=_SHANGHAI_TZ)
    else:
        return _failed_record(symbol, "INVALID_OBSERVATION")

    # Future data protection: fail closed
    if source_time > cutoff + timedelta(minutes=5):
        return _failed_record(symbol, "INVALID_SOURCE_TIME")

    cutoff_shanghai_date = cutoff.astimezone(_SHANGHAI_TZ).date()
    if parsed_sd > cutoff_shanghai_date:
        return _failed_record(symbol, "INVALID_SOURCE_TIME")

    # Freshness evaluation
    diff_seconds = abs((cutoff - source_time).total_seconds())
    if diff_seconds <= 36 * 3600:
        freshness = "RECENT"
    elif diff_seconds <= 120 * 3600:
        freshness = "DELAYED_OR_HOLIDAY"
    else:
        freshness = "STALE"

    # Change computation
    if symbol == _TREASURY_SYMBOL:
        change = (val - prev_val) * 100.0
        change_unit = "bp"
    else:
        change = (val / prev_val - 1.0) * 100.0
        change_unit = "pct"

    if not math.isfinite(change):
        return _failed_record(symbol, "INVALID_OBSERVATION")

    return {
        "category": entry.category,
        "symbol": symbol,
        "name": entry.name,
        "value": val,
        "previous_value": prev_val,
        "value_unit": entry.value_unit,
        "change": change,
        "change_unit": change_unit,
        "source": obs.source,
        "source_as_of": obs.source_as_of,
        "session_date": obs.session_date,
        "freshness": freshness,
        "error_code": None,
    }


def build_global_market(observations, cutoff: datetime, *, collected: bool = True) -> dict:
    """Build standard overnight global market payload from observations."""
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.tzinfo.utcoffset(cutoff) is None:
        raise ValueError("cutoff must be timezone-aware")

    if not collected:
        quote_records = tuple(_failed_record(sym, "NOT_COLLECTED_FOR_STAGE") for sym in GLOBAL_SYMBOLS)
        status = "not_collected"
    else:
        obs_map = {}
        if hasattr(observations, "__iter__"):
            for item in observations:
                if isinstance(item, (tuple, list)) and len(item) == 4:
                    obs_map[item[0]] = item

        records = []
        for symbol in GLOBAL_SYMBOLS:
            item = obs_map.get(symbol)
            if item is None:
                records.append(_failed_record(symbol, "INVALID_OBSERVATION"))
            else:
                records.append(_build_single_quote(item, cutoff))
        quote_records = tuple(records)

        success_count = sum(1 for q in quote_records if q.get("error_code") is None)
        if success_count == len(GLOBAL_SYMBOLS):
            status = "complete"
        elif success_count > 0:
            status = "partial"
        else:
            status = "unavailable"

    payload = {
        "status": status,
        "cutoff": cutoff.isoformat(),
        "quotes": quote_records,
    }
    payload["summary"] = summarize_overnight(payload)
    return payload


def _usable(record) -> bool:
    """Predicate for quote availability without STALE or errors."""
    return (
        isinstance(record, dict)
        and record.get("error_code") is None
        and record.get("freshness") in ("RECENT", "DELAYED_OR_HOLIDAY")
        and isinstance(record.get("change"), (int, float))
        and not isinstance(record.get("change"), bool)
        and math.isfinite(record.get("change"))
    )


def summarize_overnight(payload) -> dict:
    """Summarize overnight market facts without predictions or actions."""
    if not isinstance(payload, dict) or "quotes" not in payload or not isinstance(payload["quotes"], (list, tuple)):
        return {
            "gated": True,
            "reason": "隔夜关键数据缺失或已陈旧，本次不作方向判断。",
            "direction": None,
            "technology": None,
            "impact": None,
            "note": SUMMARY_NOTE,
        }

    quotes_by_symbol = {
        r.get("symbol"): r for r in payload["quotes"] if isinstance(r, dict) and "symbol" in r
    }

    gspc = quotes_by_symbol.get("^GSPC")
    if not _usable(gspc):
        return {
            "gated": True,
            "reason": "隔夜关键数据缺失或已陈旧，本次不作方向判断。",
            "direction": None,
            "technology": None,
            "impact": None,
            "note": SUMMARY_NOTE,
        }

    gspc_change = gspc["change"]
    if gspc_change >= 0.5:
        direction = "上涨"
    elif gspc_change <= -0.5:
        direction = "下跌"
    else:
        direction = "震荡"

    ixic = quotes_by_symbol.get("^IXIC")
    sox = quotes_by_symbol.get("^SOX")
    if _usable(ixic) and _usable(sox):
        ixic_change = ixic["change"]
        sox_change = sox["change"]
        if ixic_change >= 0.5 and sox_change >= 0.5:
            technology = "偏强"
        elif ixic_change <= -0.5 and sox_change <= -0.5:
            technology = "偏弱"
        else:
            technology = "分化"
    else:
        technology = "数据不足"

    if technology == "偏强":
        impact = "A股科技方向偏正面"
    elif technology == "偏弱":
        impact = "A股科技方向偏负面"
    elif technology == "分化":
        impact = "A股科技方向暂不明朗"
    else:
        impact = "科技方向数据不足，暂不判断"

    return {
        "gated": False,
        "reason": None,
        "direction": direction,
        "technology": technology,
        "impact": impact,
        "note": SUMMARY_NOTE,
    }


def format_overnight_lines(payload) -> tuple[str, ...]:
    """Format user-facing lines completely isolated from internal markers."""
    lines = ["🌍 隔夜全球市场"]

    status = payload.get("status") if isinstance(payload, dict) else None
    lines.append(STATUS_LABELS.get(status, STATUS_LABELS["unavailable"]))

    quotes_by_symbol = {}
    if isinstance(payload, dict) and isinstance(payload.get("quotes"), (list, tuple)):
        for q in payload["quotes"]:
            if isinstance(q, dict) and "symbol" in q:
                quotes_by_symbol[q["symbol"]] = q

    for sym in GLOBAL_SYMBOLS:
        record = quotes_by_symbol.get(sym)
        entry = INSTRUMENTS.get(sym)
        name = record.get("name") if (record and record.get("name")) else (entry.name if entry else sym)

        if record and record.get("error_code") is None and record.get("value") is not None:
            val = record["value"]
            unit = record.get("value_unit")
            if unit == "percent":
                val_str = f"{val:.2f}"
            else:
                val_str = f"{val:,.2f}"

            if unit == "points":
                unit_str = " 点"
            elif unit == "percent":
                unit_str = "%"
            elif unit == "usd_per_ounce":
                unit_str = " 美元/盎司"
            elif unit == "usd_per_barrel":
                unit_str = " 美元/桶"
            else:
                unit_str = f" {unit}" if unit else ""

            change = record.get("change", 0.0)
            change_unit = record.get("change_unit")
            if change_unit == "bp":
                change_str = f"{change:+.1f} bp"
            else:
                change_str = f"{change:+.2f}%"

            source_as_of = record.get("source_as_of")
            if source_as_of:
                dt = datetime.fromisoformat(source_as_of).astimezone(_SHANGHAI_TZ)
                time_str = f"截至 {dt.strftime('%m-%d %H:%M')}"
            else:
                sd = record.get("session_date") or ""
                time_str = f"数据日 {sd[5:]}" if len(sd) >= 5 else "数据日"

            freshness_str = FRESHNESS_LABELS.get(record.get("freshness"), "")
            lines.append(f"- {name}：{val_str}{unit_str}；较前值 {change_str}；{time_str}；{freshness_str}。")
        else:
            lines.append(f"- {name}：暂不可用。")

    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict):
        summary = summarize_overnight(payload)

    if summary.get("gated"):
        reason = summary.get("reason") or "隔夜关键数据缺失或已陈旧，本次不作方向判断。"
        lines.append(f"- {reason}")
    else:
        direction = summary.get("direction") or ""
        technology = summary.get("technology") or ""
        impact = summary.get("impact") or ""
        lines.append(f"- 美股方向：{direction}；科技：{technology}；{impact}。{SUMMARY_NOTE}")

    return tuple(lines)


def data_limit_token(payload) -> str:
    """Return data limitation token for honest defect reporting."""
    if not isinstance(payload, dict):
        return "unavailable"
    status = payload.get("status")
    if status == "unavailable":
        return "unavailable"
    if status == "not_collected":
        return "not_collected"
    return "dated_observations"