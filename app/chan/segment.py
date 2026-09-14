from dataclasses import dataclass
from typing import List
from .bi import Stroke


@dataclass
class Segment:
    strokes: List[Stroke]
    direction: str
    high: float = None
    low: float = None

    def __post_init__(self):
        if self.direction not in ("up", "down"):
            raise ValueError("direction must be 'up' or 'down'")
        known_highs = [stroke.high for stroke in self.strokes if stroke.high is not None]
        known_lows = [stroke.low for stroke in self.strokes if stroke.low is not None]
        if self.high is None and known_highs:
            self.high = max(known_highs)
        if self.low is None and known_lows:
            self.low = min(known_lows)


def build_segments(strokes: List[Stroke]) -> List[Segment]:
    """Build three-stroke segment candidates with one-stroke continuity."""
    if not strokes:
        return []

    for left, right in zip(strokes, strokes[1:]):
        if left.direction == right.direction:
            raise ValueError("adjacent strokes must alternate direction")

    segments = []
    for start in range(0, len(strokes) - 2, 2):
        window = strokes[start:start + 3]
        if len(window) < 3:
            break
        segments.append(Segment(strokes=window, direction=window[0].direction))
    return segments
