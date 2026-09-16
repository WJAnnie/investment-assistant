"""Optional historical analysis for multi-account portfolio reports.

Valuation and historical analysis are separate paths: an unavailable K-line
source must not produce a made-up technical conclusion or block valuation.
"""

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import isfinite
from zoneinfo import ZoneInfo

from app.analysis.boll import calculate_boll
from app.analysis.fundamental import (
    FundamentalPolicy,
    ValuationPolicy,
    evaluate_fundamental,
    evaluate_valuation,
)
from app.analysis.industry import (
    DEFAULT_INDUSTRY_POLICY,
    IndustryRankingPolicy,
    rank_industries,
)
from app.analysis.kdj import calculate_kdj
from app.analysis.macd import calculate_macd
from app.analysis.rsi import calculate_rsi
from app.analysis.technical_score import calculate_technical_score
from app.analysis.trend import trend_score
from app.chan.models import KLine
from app.chan.pipeline import analyze_chan
from app.decision.engine import build_decision
from app.domain.bars import BarStatus
from app.domain.evidence import EvidenceStatus
from app.domain.timeframe import Timeframe
from app.market.minute.context import load_minute_context
from app.portfolio.structure import build_structure_evidence
from app.portfolio.structure_inputs import cycle_from_dated_lines, cycle_from_minute_bars


DEFAULT_MIN_HISTORY_BARS = 30
MARKET_TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_FUNDAMENTAL_POLICY = FundamentalPolicy(
    min_roe=Decimal("8"),
    min_eps=Decimal("0"),
    min_revenue_growth=Decimal("0"),
    min_profit_growth=Decimal("0"),
    max_age_days=400,
)

DEFAULT_VALUATION_POLICY = ValuationPolicy(
    max_pe=Decimal("60"),
    max_pb=Decimal("10"),
    max_ps=Decimal("30"),
    max_age_days=30,
)


def _is_supported_a_share(code, market, instrument_type) -> bool:
    if market != "CN" or instrument_type != "stock":
        return False
    if type(code) is not str:
        return False
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return False
    if code.startswith(("6", "0", "3", "4", "8")) or code.startswith("92"):
        return True
    return False


def _finite_metric(val):
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float, Decimal)):
        if isinstance(val, float) and not isfinite(val):
            return None
        if isinstance(val, Decimal) and not val.is_finite():
            return None
        return val
    return None


def _evaluate_stock_fundamental(provider, code: str, current_time: datetime) -> dict:
    try:
        fetch_result = provider.fetch(code, current_time)
    except Exception:
        fetch_result = None

    if fetch_result is None:
        return {
            "status": "not_available",
            "reason_code": None,
            "criterion_passed": False,
            "complete": False,
            "roe_pct": None,
            "revenue_growth_pct": None,
            "net_profit_growth_pct": None,
            "eps": None,
            "pe_ttm": None,
            "pb": None,
            "ps": None,
        }

    fundamental_obs = getattr(fetch_result, "fundamental", None)
    valuation_obs = getattr(fetch_result, "valuation", None)

    try:
        fund_gate = evaluate_fundamental(fundamental_obs, current_time, DEFAULT_FUNDAMENTAL_POLICY)
    except Exception:
        fund_gate = None

    try:
        val_gate = evaluate_valuation(valuation_obs, current_time, DEFAULT_VALUATION_POLICY)
    except Exception:
        val_gate = None

    if fund_gate is None or val_gate is None:
        status = "not_available"
        reason_code = None
        complete = False
        criterion_passed = False
    elif fund_gate.stamp.status == EvidenceStatus.READY and val_gate.stamp.status == EvidenceStatus.READY:
        status = EvidenceStatus.READY.value
        reason_code = None
        complete = bool(fund_gate.complete and val_gate.complete)
        criterion_passed = bool(fund_gate.criterion_passed and val_gate.criterion_passed)
    else:
        if EvidenceStatus.FAILED in (fund_gate.stamp.status, val_gate.stamp.status):
            failed_gate = fund_gate if fund_gate.stamp.status == EvidenceStatus.FAILED else val_gate
            status = EvidenceStatus.FAILED.value
            reason_code = failed_gate.stamp.reason_code
        elif EvidenceStatus.DEGRADED in (fund_gate.stamp.status, val_gate.stamp.status):
            degraded_gate = fund_gate if fund_gate.stamp.status == EvidenceStatus.DEGRADED else val_gate
            status = EvidenceStatus.DEGRADED.value
            reason_code = degraded_gate.stamp.reason_code
        elif fund_gate.stamp.status != EvidenceStatus.READY:
            status = fund_gate.stamp.status.value
            reason_code = fund_gate.stamp.reason_code
        else:
            status = val_gate.stamp.status.value
            reason_code = val_gate.stamp.reason_code
        complete = bool(fund_gate.complete and val_gate.complete)
        criterion_passed = bool(fund_gate.criterion_passed and val_gate.criterion_passed)

    return {
        "status": status,
        "reason_code": reason_code,
        "criterion_passed": criterion_passed,
        "complete": complete,
        "roe_pct": _finite_metric(getattr(fundamental_obs, "roe_pct", None)),
        "revenue_growth_pct": _finite_metric(getattr(fundamental_obs, "revenue_growth_pct", None)),
        "net_profit_growth_pct": _finite_metric(getattr(fundamental_obs, "net_profit_growth_pct", None)),
        "eps": _finite_metric(getattr(fundamental_obs, "eps", None)),
        "pe_ttm": _finite_metric(getattr(valuation_obs, "pe_ttm", None)),
        "pb": _finite_metric(getattr(valuation_obs, "pb", None)),
        "ps": _finite_metric(getattr(valuation_obs, "ps", None)),
    }


def _latest_closed_mainland_market_date(current_time: datetime) -> date:
    from app.portfolio.valuation import PortfolioValuationRouter

    if current_time.tzinfo is None or current_time.utcoffset() is None:
        dt = current_time.replace(tzinfo=MARKET_TZ)
    else:
        dt = current_time.astimezone(MARKET_TZ)
    d = dt.date()
    t = dt.time()
    if not PortfolioValuationRouter._is_mainland_business_day(d) or t < time(15, 0):
        candidate = d - timedelta(days=1)
        while not PortfolioValuationRouter._is_mainland_business_day(candidate):
            candidate -= timedelta(days=1)
        return candidate
    return d


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
    industry_provider=None,
    fundamental_provider=None,
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
    fundamental_cache = {}
    items = []
    minute_cycles_assembled = 0

    for account in config.accounts:
        for holding in account.holdings:
            cache_key = _holding_cache_key(holding)
            if cache_key not in cache:
                minute = load_minute_context(
                    holding, snapshot_loader=minute_snapshot_loader, now=current_time,
                )
                cache[cache_key] = _load_historical_analysis(
                    holding,
                    history_loader,
                    options,
                    min_history_bars,
                    chan_min_span,
                    current_time,
                    minute_context=minute,
                )
                cache[cache_key]["minute_context"] = minute
                cache[cache_key]["next_trigger"] = minute["next_trigger"]
                if minute_snapshot_loader is not None:
                    cache[cache_key]["bar_status"] = {
                        "daily": cache[cache_key].get("bar_status", "missing"),
                        **minute["bar_status"],
                    }
            item = dict(cache[cache_key])
            if item.get("_minute_cycles_count", 0) > 0:
                minute_cycles_assembled += item["_minute_cycles_count"]
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
            if fundamental_provider is not None and _is_supported_a_share(
                holding.code, holding.market, holding.instrument_type
            ):
                if holding.code not in fundamental_cache:
                    fundamental_cache[holding.code] = _evaluate_stock_fundamental(
                        fundamental_provider, holding.code, current_time
                    )
                item["fundamental"] = dict(fundamental_cache[holding.code])
            items.append(item)

    for item in items:
        item.pop("_minute_cycles_count", None)

    benchmark = None
    if history_loader is not None:
        benchmark = _load_benchmark_analysis(
            history_loader,
            options,
            min_history_bars,
            chan_min_span,
            current_time,
        )
        if isinstance(benchmark, dict):
            benchmark.pop("_minute_cycles_count", None)

    ranking = None
    if industry_provider is not None:
        market_date = _latest_closed_mainland_market_date(current_time)
        try:
            fetch_result = industry_provider.fetch(cutoff=current_time, market_date=market_date)
        except Exception:
            fetch_result = None
        if fetch_result is not None:
            try:
                obs = (
                    getattr(fetch_result, "observations", None)
                    if not isinstance(fetch_result, Mapping)
                    else fetch_result.get("observations")
                )
                if obs is None:
                    obs = ()
                ranking = rank_industries(
                    obs,
                    current_time,
                    DEFAULT_INDUSTRY_POLICY,
                )
            except Exception:
                ranking = None

    ready_count = sum(item["status"] == "ready" for item in items)
    minute_ready = sum(item["minute_context"]["status"] == "ready" for item in items)
    data_limits_industry = (
        "available"
        if ranking is not None and ranking.stamp.status is EvidenceStatus.READY
        else "configured_labels_only"
    )

    eligible_fundamental = sum(1 for it in items if "fundamental" in it)
    ready_fundamental = sum(
        1 for it in items
        if "fundamental" in it and str(it["fundamental"].get("status", "")).upper() == "READY"
    )
    passed_fundamental = sum(
        1 for it in items
        if "fundamental" in it and it["fundamental"].get("criterion_passed") is True
    )
    data_limits_fundamental = (
        "available"
        if fundamental_provider is not None and eligible_fundamental > 0 and ready_fundamental == eligible_fundamental
        else "unavailable"
    )

    payload = {
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
            "fundamental": data_limits_fundamental,
            "industry": data_limits_industry,
            "news": "unavailable",
            "minute": "closure_evidence_only" if items and minute_ready == len(items) else "unavailable",
            "structure": "dated_and_minute_cycles" if minute_cycles_assembled > 0 else "dated_cycles_only",
        },
        "data_cutoff": _market_now(current_time).isoformat(),
    }
    if fundamental_provider is not None:
        payload["fundamental"] = {
            "status": "available" if (eligible_fundamental > 0 and ready_fundamental == eligible_fundamental) else "not_available",
            "eligible": eligible_fundamental,
            "ready": ready_fundamental,
            "criterion_passed": passed_fundamental,
        }
    if industry_provider is not None:
        if ranking is not None:
            payload["industry_ranking"] = {
                "status": ranking.stamp.status.value,
                "reason_code": ranking.stamp.reason_code,
                "as_of": ranking.stamp.as_of.isoformat(),
                "coverage": ranking.coverage,
                "required": ranking.required,
                "items": tuple(
                    {
                        "code": it.code,
                        "name": it.name,
                        "rank": it.rank,
                        "score": it.score,
                    }
                    for it in ranking.items
                ),
            }
        else:
            payload["industry_ranking"] = {
                "status": "not_available",
                "reason_code": None,
                "as_of": None,
                "coverage": 0,
                "required": DEFAULT_INDUSTRY_POLICY.min_coverage,
                "items": (),
            }
    return payload


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
    minute_context=None,
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
            minute_context=minute_context,
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

    minute_tfs = {
        Timeframe.MIN_120,
        Timeframe.MIN_30,
        Timeframe.MIN_15,
        Timeframe.MIN_5,
    }
    minute_cycles_count = (
        sum(1 for ev in structure.confirm.per_cycle if ev.timeframe in minute_tfs)
        if structure is not None
        else 0
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
        "_minute_cycles_count": minute_cycles_count,
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


def _structure_evidence(
    code, market, period, lines, cutoff, trend_confirm, minute_context=None
):
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

    if isinstance(minute_context, Mapping):
        minute_cycles = minute_context.get("cycles")
        if isinstance(minute_cycles, Mapping):
            minute_tfs = (
                Timeframe.MIN_120,
                Timeframe.MIN_30,
                Timeframe.MIN_15,
                Timeframe.MIN_5,
            )
            for tf in minute_tfs:
                try:
                    c_info = minute_cycles.get(tf.value)
                    if not isinstance(c_info, Mapping):
                        continue
                    closed_lines = c_info.get("closed_lines")
                    if not isinstance(closed_lines, (list, tuple)) or not closed_lines:
                        continue
                    if not all(isinstance(bar, KLine) for bar in closed_lines):
                        continue
                    raw_status = c_info.get("status")
                    if isinstance(raw_status, BarStatus):
                        status_enum = raw_status
                    elif isinstance(raw_status, str):
                        status_enum = BarStatus(raw_status)
                    else:
                        continue
                    min_cycle = cycle_from_minute_bars(
                        tf, closed_lines, status=status_enum, source="minute_context"
                    )
                    cycles[tf] = min_cycle
                except Exception:
                    continue

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
