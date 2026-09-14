"""Connected Chan-structure analysis pipeline."""

from .bi import build_strokes
from .divergence import DivergenceResult, detect_macd_divergence
from .fractal import detect_fenxing
from .inclusion import normalize_k_lines
from .segment import build_segments
from .signal import ChanSignalEngine
from .zhongshu import find_zhongshu


def analyze_chan(
    lines,
    buy_setup=None,
    prices=None,
    macd_series=None,
    min_span=4,
):
    """Run K-lines through the Chan structure stages and return an audit trail."""
    normalized_lines = normalize_k_lines(lines)
    fractals = detect_fenxing(normalized_lines, normalize_inclusion=False)
    strokes = build_strokes(fractals, min_span=min_span)
    segments = build_segments(strokes)
    zhongshu = find_zhongshu(segments)

    if prices is not None and macd_series is not None:
        divergence = detect_macd_divergence(prices, macd_series)
    else:
        divergence = DivergenceResult(False, "not evaluated", 0)

    context = dict(buy_setup or {})
    context.setdefault("zhongshu", bool(zhongshu))
    context.setdefault("divergence", divergence)
    signal = ChanSignalEngine().evaluate(context)
    return {
        "normalized_lines": normalized_lines,
        "fractals": fractals,
        "strokes": strokes,
        "segments": segments,
        "zhongshu": zhongshu,
        "divergence": divergence,
        "context": context,
        "signal": signal,
    }
