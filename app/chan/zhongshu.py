from dataclasses import dataclass
from typing import List, Tuple
from .segment import Segment


@dataclass
class ZhongShu:
    start_index: int
    end_index: int
    high: float
    low: float

    @property
    def center(self) -> float:
        return (self.high + self.low) / 2


def find_zhongshu(segments: List[Segment]) -> List[ZhongShu]:
    """Find three-segment range intersections that form ZhongShu candidates."""
    result = []
    for index in range(len(segments) - 2):
        window = segments[index:index + 3]
        if any(segment.high is None or segment.low is None for segment in window):
            continue

        high = min(segment.high for segment in window)
        low = max(segment.low for segment in window)
        if low >= high:
            continue

        start = index
        end = index + 2
        if window[0].strokes:
            start = window[0].strokes[0].start_index
        if window[-1].strokes:
            end = window[-1].strokes[-1].end_index
        result.append(ZhongShu(start, end, high, low))
    return result
