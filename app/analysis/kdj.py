"""KDJ indicator module."""


def calculate_kdj(highs, lows, closes, period=9):
    if not isinstance(period, int) or isinstance(period, bool) or period <= 0:
        raise ValueError("period must be a positive integer")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows and closes must have the same length")
    if len(closes) < period:
        return {"k": None, "d": None, "j": None}

    window_high = max(highs[-period:])
    window_low = min(lows[-period:])

    if window_high == window_low:
        rsv = 50
    else:
        rsv = (closes[-1] - window_low) / (window_high - window_low) * 100

    k = rsv
    d = k
    j = 3 * k - 2 * d

    return {
        "k": round(k, 2),
        "d": round(d, 2),
        "j": round(j, 2),
    }
