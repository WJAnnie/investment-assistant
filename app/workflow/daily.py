from datetime import datetime

from app.analysis.kdj import calculate_kdj
from app.analysis.macd import calculate_macd
from app.analysis.rsi import calculate_rsi
from app.analysis.technical_score import calculate_technical_score
from app.analysis.trend import trend_score
from app.chan.pipeline import analyze_chan
from app.chan.signal import ChanSignalEngine
from app.decision.engine import build_decision
from app.market.models import Quote
from app.report.formatter import format_daily_report


def daily_report_job(
    code=None,
    prices=None,
    highs=None,
    lows=None,
    k_lines=None,
    chan_min_span=4,
    chan_context=None,
    collector=None,
    notifier=None,
    now=None,
    fetch_history=False,
    history_start=None,
    history_end=None,
    history_period="daily",
    history_adjust="",
):
    """Run the deterministic research pipeline with injectable I/O boundaries."""
    current_time = now or datetime.now().astimezone()
    if prices is None and fetch_history:
        if k_lines is None:
            if collector is None or not code:
                raise ValueError("collector and code are required when fetch_history is enabled")
            k_lines = collector.get_klines(
                code,
                start_date=history_start,
                end_date=history_end,
                period=history_period,
                adjust=history_adjust,
            )
        try:
            prices = [line.close for line in k_lines]
            highs = highs if highs is not None else [line.high for line in k_lines]
            lows = lows if lows is not None else [line.low for line in k_lines]
        except (AttributeError, TypeError):
            raise ValueError("k_lines must contain KLine values") from None
    if prices is None:
        return {"time": current_time.isoformat(), "status": "initialized"}
    if not code:
        raise ValueError("code is required when prices are supplied")
    if len(prices) < 2:
        raise ValueError("at least two prices are required")

    highs = highs if highs is not None else [price for price in prices]
    lows = lows if lows is not None else [price for price in prices]
    if not (len(prices) == len(highs) == len(lows)):
        raise ValueError("prices, highs and lows must have the same length")

    quote = (
        collector.get_quote(code)
        if collector is not None
        else Quote(code, code, float(prices[-1]), 0.0, current_time.isoformat())
    )
    macd = calculate_macd(prices)
    rsi_values = calculate_rsi(prices)
    kdj = calculate_kdj(highs, lows, prices)
    trend = trend_score(prices)
    technical = calculate_technical_score(
        macd=macd,
        rsi={"value": rsi_values[-1] if rsi_values else None},
        kdj=kdj,
        trend=trend,
    )
    chan_analysis = None
    if k_lines is not None:
        chan_analysis = analyze_chan(
            k_lines,
            buy_setup=chan_context,
            prices=prices,
            macd_series=macd["hist"],
            min_span=chan_min_span,
        )
        signal = chan_analysis["signal"]
    else:
        signal = ChanSignalEngine().evaluate(chan_context or {})
    decision = build_decision(signal, technical["score"])
    report = format_daily_report(quote, technical, signal, decision)

    notified = False
    notification_error = None
    if notifier is not None:
        try:
            notified = bool(notifier.send(report))
        except Exception as exc:
            notification_error = str(exc)

    result = {
        "time": current_time.isoformat(),
        "status": "completed",
        "quote": quote,
        "technical": technical,
        "signal": signal,
        "decision": decision,
        "report": report,
        "notified": notified,
    }
    if chan_analysis is not None:
        result["chan"] = chan_analysis
    if notification_error:
        result["notification_error"] = notification_error
    return result
