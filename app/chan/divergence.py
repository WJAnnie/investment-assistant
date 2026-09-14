from dataclasses import dataclass


@dataclass
class DivergenceResult:
    detected: bool
    reason: str
    score: int
    kind: str = None


def detect_macd_divergence(price_series, macd_series, kind=None) -> DivergenceResult:
    """Detect a basic two-swing price/MACD divergence."""
    if len(price_series) != len(macd_series):
        raise ValueError("price_series and macd_series must have the same length")
    if kind not in (None, "bullish", "bearish"):
        raise ValueError("kind must be bullish, bearish or None")
    if len(price_series) < 6:
        return DivergenceResult(False, "not enough structure", 0)

    midpoint = len(price_series) // 2
    first_low_index = min(range(midpoint), key=lambda index: price_series[index])
    second_low_index = min(
        range(midpoint, len(price_series)), key=lambda index: price_series[index]
    )
    first_high_index = max(range(midpoint), key=lambda index: price_series[index])
    second_high_index = max(
        range(midpoint, len(price_series)), key=lambda index: price_series[index]
    )

    bullish = (
        price_series[second_low_index] < price_series[first_low_index]
        and macd_series[second_low_index] > macd_series[first_low_index]
    )
    bearish = (
        price_series[second_high_index] > price_series[first_high_index]
        and macd_series[second_high_index] < macd_series[first_high_index]
    )

    if kind == "bullish":
        bearish = False
    if kind == "bearish":
        bullish = False
    if bullish:
        return DivergenceResult(True, "lower price low with stronger MACD", 70, "bullish")
    if bearish:
        return DivergenceResult(True, "higher price high with weaker MACD", 70, "bearish")
    return DivergenceResult(False, "no price/MACD divergence", 0)
