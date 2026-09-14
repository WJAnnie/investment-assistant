"""Trend analysis module."""


def trend_direction(prices, short_window=5, long_window=20):
    if short_window <= 0 or long_window <= 0 or short_window > long_window:
        raise ValueError("windows must be positive and short_window <= long_window")
    if len(prices) < long_window:
        return "UNKNOWN"

    short_avg = sum(prices[-short_window:]) / short_window
    long_avg = sum(prices[-long_window:]) / long_window

    if short_avg > long_avg:
        return "UP"
    if short_avg < long_avg:
        return "DOWN"
    return "SIDEWAYS"


def trend_score(prices):
    direction = trend_direction(prices)

    return {
        "direction": direction,
        "score": {
            "UP": 80,
            "SIDEWAYS": 60,
            "DOWN": 30,
            "UNKNOWN": 0,
        }[direction],
    }
