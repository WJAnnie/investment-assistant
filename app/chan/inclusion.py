"""K-line inclusion relationship handling.

This module normalizes contained candles before fractal detection.
"""

from dataclasses import replace

from .models import KLine


def merge_inclusion(left: KLine, right: KLine, direction=None):
    """Merge two K lines when one contains the other.

    Direction should be provided by the caller in production according to
    previous trend. This keeps the primitive deterministic.
    """
    if direction not in (None, "up", "down"):
        raise ValueError("direction must be 'up', 'down' or None")

    if direction == "up":
        high = max(left.high, right.high)
        low = max(left.low, right.low)
    elif direction == "down":
        high = min(left.high, right.high)
        low = min(left.low, right.low)
    else:
        high = max(left.high, right.high)
        low = min(left.low, right.low)

    close = right.close
    return replace(
        right,
        open=left.open,
        high=high,
        low=low,
        close=close,
        volume=left.volume + right.volume,
    )


def has_inclusion(left, right):
    return (left.high >= right.high and left.low <= right.low) or (
        right.high >= left.high and right.low <= left.low
    )


def normalize_k_lines(lines):
    """Merge contained candles using the direction implied by their closes."""
    normalized = []
    for line in lines:
        if not isinstance(line, KLine):
            raise TypeError("lines must contain KLine values")
        if not normalized or not has_inclusion(normalized[-1], line):
            normalized.append(line)
            continue

        previous = normalized[-1]
        direction = "up" if line.close >= previous.close else "down"
        normalized[-1] = merge_inclusion(previous, line, direction)
    return normalized
