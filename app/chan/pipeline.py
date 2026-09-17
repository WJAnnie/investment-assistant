"""Connected Chan-structure analysis pipeline."""

from collections.abc import Mapping
from decimal import Decimal
import math
from typing import Any

from .bi import build_strokes
from .divergence import DivergenceResult, detect_macd_divergence
from .fractal import detect_fenxing
from .inclusion import normalize_k_lines
from .segment import build_segments
from .signal import ChanSignalEngine
from .zhongshu import find_zhongshu


def derive_buy_flags(chan_result: dict, *, close: Any) -> dict[str, bool]:
    """Derive buy-point evidence flags from verified Chan structure and close price.

    Pure function producing boolean evidence flags:
    - zhongshu_breakout: last zhongshu exists and close > last_zhongshu.high
    - second_buy: last zhongshu exists and low <= close <= high (stepping back into zhongshu)
    - class_second: last zhongshu exists and divergence.detected is True and close > last_zhongshu.center
    - first_buy: divergence.detected is True and last zhongshu exists and close < last_zhongshu.low
    - third_buy: zhongshu_breakout is True (breakout precondition for 3rd buy)
    """
    if not isinstance(chan_result, dict):
        raise TypeError(f"chan_result must be a dict, got {type(chan_result).__name__}")
    if "zhongshu" not in chan_result:
        raise ValueError("chan_result must contain 'zhongshu' key")
    if "divergence" not in chan_result:
        raise ValueError("chan_result must contain 'divergence' key")

    zhongshu_list = chan_result["zhongshu"]
    if not isinstance(zhongshu_list, (list, tuple)):
        raise TypeError(f"zhongshu must be a list or tuple, got {type(zhongshu_list).__name__}")

    divergence = chan_result["divergence"]
    if not hasattr(divergence, "detected") and not (
        isinstance(divergence, Mapping) and "detected" in divergence
    ):
        raise ValueError("divergence must have 'detected' attribute or key")

    if close is None or isinstance(close, bool):
        raise TypeError("close must be a finite positive number, cannot be None or bool")
    if not isinstance(close, (int, float, Decimal)):
        raise TypeError(f"close must be a numeric type, got {type(close).__name__}")
    if isinstance(close, float) and not math.isfinite(close):
        raise ValueError("close must be a finite number")
    if isinstance(close, Decimal) and not close.is_finite():
        raise ValueError("close must be a finite number")
    if close <= 0:
        raise ValueError("close must be positive")

    if not zhongshu_list:
        return {
            "zhongshu_breakout": False,
            "second_buy": False,
            "class_second": False,
            "first_buy": False,
            "third_buy": False,
        }

    last_zs = zhongshu_list[-1]
    if isinstance(last_zs, Mapping):
        zs_high = float(last_zs["high"])
        zs_low = float(last_zs["low"])
        zs_center = float(last_zs.get("center", (zs_high + zs_low) / 2))
    else:
        zs_high = float(last_zs.high)
        zs_low = float(last_zs.low)
        zs_center = float(last_zs.center)

    close_val = float(close)
    div_detected = (
        bool(divergence["detected"])
        if isinstance(divergence, Mapping)
        else bool(divergence.detected)
    )

    zhongshu_breakout = bool(close_val > zs_high)
    second_buy = bool(zs_low <= close_val <= zs_high)
    class_second = bool(div_detected and close_val > zs_center)
    first_buy = bool(div_detected and close_val < zs_low)
    third_buy = bool(zhongshu_breakout)

    return {
        "zhongshu_breakout": zhongshu_breakout,
        "second_buy": second_buy,
        "class_second": class_second,
        "first_buy": first_buy,
        "third_buy": third_buy,
    }


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

    chan_partial = {
        "normalized_lines": normalized_lines,
        "fractals": fractals,
        "strokes": strokes,
        "segments": segments,
        "zhongshu": zhongshu,
        "divergence": divergence,
    }

    derived_flags = {}
    latest_close = None
    if lines:
        last_item = lines[-1]
        latest_close = getattr(last_item, "close", None)
        if latest_close is None and isinstance(last_item, Mapping):
            latest_close = last_item.get("close")
    if latest_close is None and prices:
        latest_close = prices[-1]

    if latest_close is not None:
        try:
            derived_flags = derive_buy_flags(chan_partial, close=latest_close)
        except (TypeError, ValueError):
            derived_flags = {}

    context = dict(derived_flags)
    context.setdefault("zhongshu", bool(zhongshu))
    context.setdefault("divergence", divergence)
    if buy_setup:
        context.update(buy_setup)

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


__all__ = ["analyze_chan", "derive_buy_flags"]

