from typing import List
from .models import KLine, FenXing
from .inclusion import normalize_k_lines


def detect_fenxing(lines: List[KLine], normalize_inclusion=True) -> List[FenXing]:
    """Detect top/bottom fractals after optional inclusion normalization."""
    if normalize_inclusion:
        lines = normalize_k_lines(lines)
    result = []
    for i in range(1, len(lines)-1):
        prev_line = lines[i-1]
        cur = lines[i]
        next_line = lines[i+1]

        if cur.high > prev_line.high and cur.high > next_line.high:
            result.append(FenXing(i, cur.high, "top"))

        if cur.low < prev_line.low and cur.low < next_line.low:
            result.append(FenXing(i, cur.low, "bottom"))

    return result
