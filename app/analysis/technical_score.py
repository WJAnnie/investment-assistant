"""Combine technical indicators into a unified score.

This score is auxiliary research evidence only: it never triggers a BUY/WAIT
decision by itself. Final actions must pass the seven-gate decision process
(Signal != Decision), so callers may only use this score as one supporting
input, not as a standalone decision source.
"""


def _latest(value):
    if isinstance(value, (list, tuple)):
        return value[-1] if value else None
    return value


def calculate_technical_score(macd=None, rsi=None, kdj=None, trend=None):
    score = 50
    signals = []

    if macd:
        hist = _latest(macd.get("hist", 0))
        if hist is not None and hist > 0:
            score += 10
            signals.append("MACD修复")

    if rsi:
        value = _latest(rsi.get("value"))
        if value is not None and 40 < value < 70:
            score += 10
            signals.append("RSI健康")

    if kdj:
        k = _latest(kdj.get("k"))
        j = _latest(kdj.get("j"))
        if k is not None and j is not None and j > k:
            score += 10
            signals.append("KDJ改善")

    if trend:
        score += max(0, min(trend.get("score", 0), 100)) * 0.25
        if trend.get("direction") == "UP":
            signals.append("趋势向上")

    return {
        "score": min(round(score), 100),
        "signals": signals,
    }
