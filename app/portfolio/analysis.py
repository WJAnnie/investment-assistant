"""Optional historical analysis for multi-account portfolio reports.

Valuation and historical analysis are separate paths: an unavailable K-line
source must not produce a made-up technical conclusion or block valuation.
"""

from datetime import date, datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from app.analysis.boll import calculate_boll
from app.analysis.kdj import calculate_kdj
from app.analysis.macd import calculate_macd
from app.analysis.rsi import calculate_rsi
from app.analysis.technical_score import calculate_technical_score
from app.analysis.trend import trend_score
from app.chan.pipeline import analyze_chan
from app.decision.engine import build_decision
from app.domain.timeframe import Timeframe
from app.market.minute.context import load_minute_context
from app.portfolio.structure import build_structure_evidence
from app.portfolio.structure_inputs import cycle_from_dated_lines


DEFAULT_MIN_HISTORY_BARS = 30
MARKET_TZ = ZoneInfo("Asia/Shanghai")


def analyze_portfolio(
    config,
    snapshot,
    history_loader=None,
    now=None,
    history_start=None,
    history_end=None,
    history_period="daily",
    history_adjust="",
    min_history_bars=DEFAULT_MIN_HISTORY_BARS,
    chan_min_span=4,
    minute_snapshot_loader=None,
):
    """Analyze each holding when enough historical bars are available."""
    if not isinstance(min_history_bars, int) or isinstance(min_history_bars, bool):
        raise ValueError("min_history_bars must be an integer")
    if min_history_bars < 2:
        raise ValueError("min_history_bars must be at least 2")

    current_time = _market_now(now if now is not None else datetime.now(MARKET_TZ))
    options = _history_options(
        current_time,
        history_start=history_start,
        history_end=history_end,
        history_period=history_period,
        history_adjust=history_adjust,
    )
    snapshot_items = {
        (account.account.account_id, item.holding.code): item
        for account in snapshot.accounts
        for item in account.holdings
    }
    cache = {}
    items = []

    for account in config.accounts:
        for holding in account.holdings:
            cache_key = _holding_cache_key(holding)
            if cache_key not in cache:
                cache[cache_key] = _load_historical_analysis(
                    holding,
                    history_loader,
                    options,
                    min_history_bars,
                    chan_min_span,
                    current_time,
                )
                minute = load_minute_context(
                    holding, snapshot_loader=minute_snapshot_loader, now=current_time,
                )
                cache[cache_key]["minute_context"] = minute
                cache[cache_key]["next_trigger"] = minute["next_trigger"]
                if minute_snapshot_loader is not None:
                    cache[cache_key]["bar_status"] = {
                        "daily": cache[cache_key].get("bar_status", "missing"),
                        **minute["bar_status"],
                    }
            item = dict(cache[cache_key])
            item["account_id"] = account.account_id
            item["code"] = holding.code
            snapshot_item = snapshot_items.get((account.account_id, holding.code))
            if snapshot_item is not None and item["status"] == "ready":
                item["decision"] = _decision_summary(
                    item["signal"],
                    item["technical"]["score"],
                    current_position=float(snapshot_item.account_weight),
                    risk_blocked="高集中度风险" in snapshot_item.warnings,
                )
            items.append(item)

    benchmark = None
    if history_loader is not None:
        benchmark = _load_benchmark_analysis(
            history_loader,
            options,
            min_history_bars,
            chan_min_span,
            current_time,
        )

    ready_count = sum(item["status"] == "ready" for item in items)
    minute_ready = sum(item["minute_context"]["status"] == "ready" for item in items)
    return {
        "items": tuple(items),
        "benchmark": benchmark,
        "coverage": {
            "ready": ready_count,
            "total": len(items),
            "unavailable": len(items) - ready_count,
        },
        "minute_coverage": {
            "ready": minute_ready,
            "total": len(items),
            "unavailable": len(items) - minute_ready,
        },
        "data_limits": {
            "fundamental": "unavailable",
            "industry": "configured_labels_only",
            "news": "unavailable",
            "minute": "closure_evidence_only" if items and minute_ready == len(items) else "unavailable",
            "structure": "dated_cycles_only",
        },
        "data_cutoff": _market_now(current_time).isoformat(),
    }


def _history_options(now, history_start, history_end, history_period, history_adjust):
    local_date = _market_now(now).date()
    return {
        "start_date": history_start or (local_date - timedelta(days=370)).strftime("%Y%m%d"),
        "end_date": history_end or local_date.strftime("%Y%m%d"),
        "period": history_period,
        "adjust": history_adjust,
    }


def _holding_cache_key(holding):
    return (
        holding.code,
        holding.market,
        holding.valuation_mode,
        holding.instrument_type,
    )


def _load_historical_analysis(
    holding,
    history_loader,
    options,
    min_history_bars,
    chan_min_span,
    current_time,
):
    if history_loader is None:
        return _unavailable("历史K线接口未配置")
    try:
        lines = history_loader(holding, **options)
    except Exception as exc:
        return _unavailable(f"历史K线获取失败：{type(exc).__name__}: {exc}")
    try:
        lines, excluded_incomplete_bars = _filter_completed_lines(
            lines,
            current_time,
            options["period"],
            getattr(holding, "market", None),
        )
    except (AttributeError, TypeError, ValueError, ArithmeticError) as exc:
        return _unavailable(f"历史K线校验失败：{type(exc).__name__}: {exc}")
    if not isinstance(lines, list) or len(lines) < min_history_bars:
        count = len(lines) if isinstance(lines, list) else 0
        period_label = "当周" if options.get("period") == "weekly" else "当日"
        suffix = (
            f"，已排除{excluded_incomplete_bars}根未完成{period_label}K线"
            if excluded_incomplete_bars else ""
        )
        return _unavailable(
            f"历史K线不足（{count}根，至少需要{min_history_bars}根{suffix}）"
        )
    try:
        for line in lines:
            ohlc = (line.open, line.high, line.low, line.close)
            if any(isinstance(value, bool) or not isfinite(value) or value <= 0 for value in ohlc):
                raise ValueError("OHLC 必须是有限正数，不能是布尔值")
            if not (line.low <= line.open <= line.high and line.low <= line.close <= line.high):
                raise ValueError("开盘价和收盘价必须位于最高价和最低价之间")
        prices = [float(line.close) for line in lines]
        highs = [float(line.high) for line in lines]
        lows = [float(line.low) for line in lines]

        macd = calculate_macd(prices)
        rsi_values = calculate_rsi(prices)
        kdj = calculate_kdj(highs, lows, prices)
        boll = calculate_boll(prices)
        trend = trend_score(prices)
        technical = calculate_technical_score(
            macd=macd,
            rsi={"value": rsi_values[-1] if rsi_values else None},
            kdj=kdj,
            trend=trend,
        )
        structure = _structure_evidence(
            holding.code,
            holding.market,
            options["period"],
            lines,
            current_time,
            trend["direction"] == "UP",
        )
        chan = analyze_chan(
            lines,
            buy_setup={
                "trend_confirm": trend["direction"] == "UP",
                "multi_cycle_confirm": structure.ready if structure is not None else False,
            },
            prices=prices,
            macd_series=macd["hist"],
            min_span=chan_min_span,
        )
    except (AttributeError, TypeError, ValueError, ArithmeticError) as exc:
        return _unavailable(f"历史K线分析失败：{type(exc).__name__}: {exc}")

    structure_payload = (
        {
            "outcome": structure.outcome.value,
            "ready": structure.ready,
            "limit": structure.limit,
            "reason_code": structure.reason_code,
            "blocked_by": tuple(structure.blocked_by),
            "per_cycle_status": dict(structure.per_cycle_status),
            "stale_cycles": tuple(structure.stale_cycles),
            "insufficient_cycles": tuple(structure.insufficient_cycles),
            "missing_cycles": tuple(
                name for name, s in structure.per_cycle_status.items() if s == "missing"
            ),
        }
        if structure is not None
        else None
    )

    signal = chan["signal"]
    return {
        "status": "ready",
        "bars": len(lines),
        "as_of": str(lines[-1].time),
        "bar_status": "completed_only" if excluded_incomplete_bars else "complete",
        "excluded_incomplete_bars": excluded_incomplete_bars,
        "technical": {
            "score": technical["score"],
            "signals": tuple(technical["signals"]),
            "trend": trend,
            "macd_hist": _latest(macd["hist"]),
            "rsi": _latest(rsi_values),
            "kdj": {"k": kdj["k"], "d": kdj["d"], "j": kdj["j"]},
            "boll": {
                "upper": _latest(boll["upper"]),
                "middle": _latest(boll["middle"]),
                "lower": _latest(boll["lower"]),
                "close_position": _boll_position(
                    prices[-1], _latest(boll["upper"]), _latest(boll["lower"])
                ),
            },
        },
        "chan": {
            "fractals": len(chan["fractals"]),
            "strokes": len(chan["strokes"]),
            "segments": len(chan["segments"]),
            "zhongshu": len(chan["zhongshu"]),
            "divergence": {
                "detected": chan["divergence"].detected,
                "kind": chan["divergence"].kind,
                "reason": chan["divergence"].reason,
            },
        },
        "signal": signal.value,
        "decision": _decision_summary(signal.value, technical["score"]),
        "structure": structure_payload,
    }


def _load_benchmark_analysis(
    history_loader, options, min_history_bars, chan_min_span, current_time
):
    try:
        lines = history_loader("HK.HSTECH", **options)
    except Exception as exc:
        return {
            "code": "HK.HSTECH",
            **_unavailable(f"恒生科技历史K线获取失败：{type(exc).__name__}: {exc}"),
        }
    benchmark = type(
        "Benchmark",
        (),
        {
            "code": "HK.HSTECH",
            "market": "HK",
            "valuation_mode": "exchange",
            "instrument_type": "index",
        },
    )()
    result = _load_historical_analysis(
        benchmark,
        lambda _holding, **_options: lines,
        options,
        min_history_bars,
        chan_min_span,
        current_time,
    )
    result["code"] = "HK.HSTECH"
    return result


def _decision_summary(signal, technical_score, current_position=0.0, risk_blocked=False):
    from app.chan.signal import ChanSignal

    signal_enum = signal if isinstance(signal, ChanSignal) else ChanSignal(signal)
    decision = build_decision(
        signal_enum,
        technical_score,
        current_position=current_position,
        risk_blocked=risk_blocked,
    )
    return {
        "action": decision.action,
        "position_percent": decision.position_percent,
        "reasons": tuple(decision.reasons),
        "auto_execute": decision.auto_execute,
        "blocked_by": tuple(decision.blocked_by),
        "new_evidence": tuple(decision.new_evidence),
    }


def _structure_evidence(code, market, period, lines, cutoff, trend_confirm):
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware datetime")
    if not isinstance(lines, (list, tuple)) or not lines:
        return None
    if period == "weekly":
        cycle = cycle_from_dated_lines(
            Timeframe.WEEKLY, lines, market=market, source="history_weekly"
        )
        cycles = {Timeframe.WEEKLY: cycle}
    elif period == "daily":
        cycle = cycle_from_dated_lines(
            Timeframe.DAILY, lines, market=market, source="history_daily"
        )
        cycles = {Timeframe.DAILY: cycle}
    else:
        return None

    if not cycles:
        return None

    return build_structure_evidence(
        code=code,
        market=market,
        cycles=cycles,
        cutoff=cutoff,
        core_signal=None,
        trend_confirm=trend_confirm,
    )


def _unavailable(reason):
    return {"status": "unavailable", "reason": reason, "decision": None, "structure": None}


def _latest(values):
    return values[-1] if values else None


def _boll_position(price, upper, lower):
    if upper is None or lower is None or upper == lower:
        return None
    return round((price - lower) / (upper - lower), 4)


def _market_now(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(MARKET_TZ)


def _filter_completed_lines(lines, current_time, period, market):
    if period not in ("daily", "weekly"):
        raise ValueError("非日线或周线周期尚无经过验证的闭合契约")
    if market not in ("CN", "HK"):
        raise ValueError(f"该市场尚无经过验证的{period}闭合契约")
    if not isinstance(lines, list) or not lines:
        return lines, 0
    local_now = _market_now(current_time)
    current_date = local_now.date()
    filtered = []
    excluded = 0
    previous_date = None
    if period == "daily":
        cutoff = time(16, 10) if market == "HK" else time(15, 0)
        for line in lines:
            line_date = _line_date(getattr(line, "time", None))
            if line_date is None:
                raise ValueError("日K线日期无法解析")
            if previous_date is not None and line_date <= previous_date:
                raise ValueError("日K线日期必须严格递增且不得重复")
            previous_date = line_date
            is_future = line_date > current_date
            is_incomplete = line_date == current_date and local_now.time() < cutoff
            if is_future or is_incomplete:
                excluded += 1
                continue
            filtered.append(line)
    elif period == "weekly":
        current_iso = (current_date.isocalendar().year, current_date.isocalendar().week)
        previous_iso = None
        for line in lines:
            line_date = _line_date(getattr(line, "time", None))
            if line_date is None:
                raise ValueError("周K线日期无法解析")
            if previous_date is not None and line_date <= previous_date:
                raise ValueError("周K线日期必须严格递增且不得重复")
            line_iso = (line_date.isocalendar().year, line_date.isocalendar().week)
            if previous_iso is not None and line_iso <= previous_iso:
                raise ValueError("周K线所属周必须严格递增且不得重复")
            previous_date = line_date
            previous_iso = line_iso
            if line_iso >= current_iso:
                excluded += 1
                continue
            filtered.append(line)
    return filtered, excluded


def _line_date(value):
    if isinstance(value, datetime):
        return _market_now(value).date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%Y/%m/%d").date()
    except ValueError:
        pass
    try:
        timestamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _market_now(timestamp).date()
