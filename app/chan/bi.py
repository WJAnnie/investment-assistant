"""Chan theory bi (stroke) construction primitives."""

from dataclasses import dataclass
from typing import List

from .models import FenXing


@dataclass
class Stroke:
    start_index: int
    end_index: int
    direction: str
    high: float = None
    low: float = None

    def __post_init__(self):
        if self.direction not in ("up", "down"):
            raise ValueError("direction must be 'up' or 'down'")
        if self.start_index >= self.end_index:
            raise ValueError("start_index must be less than end_index")

    @property
    def length(self):
        return self.end_index - self.start_index


def build_stroke(start_index, end_index, direction, high=None, low=None):
    return Stroke(start_index, end_index, direction, high, low)


def _alternating_fractals(fenxings: List[FenXing]) -> List[FenXing]:
    result = []
    for fenxing in sorted(fenxings, key=lambda item: item.index):
        if fenxing.kind not in ("top", "bottom"):
            raise ValueError("fenxing kind must be 'top' or 'bottom'")
        if not result or result[-1].kind != fenxing.kind:
            result.append(fenxing)
            continue

        is_more_extreme = (
            fenxing.price >= result[-1].price
            if fenxing.kind == "top"
            else fenxing.price <= result[-1].price
        )
        if is_more_extreme:
            result[-1] = fenxing
    return result


def build_strokes(fenxings: List[FenXing], min_span=4) -> List[Stroke]:
    """Build candidate strokes from alternating fractals."""
    if min_span <= 0:
        raise ValueError("min_span must be positive")

    cleaned = _alternating_fractals(fenxings)
    selected = []
    for fenxing in cleaned:
        if not selected:
            selected.append(fenxing)
            continue
        if fenxing.kind == selected[-1].kind:
            is_more_extreme = (
                fenxing.price >= selected[-1].price
                if fenxing.kind == "top"
                else fenxing.price <= selected[-1].price
            )
            if is_more_extreme:
                selected[-1] = fenxing
            continue
        if fenxing.index - selected[-1].index >= min_span:
            selected.append(fenxing)

    strokes = []
    for left, right in zip(selected, selected[1:]):
        if left.kind == "bottom" and right.kind == "top":
            direction = "up"
        elif left.kind == "top" and right.kind == "bottom":
            direction = "down"
        else:
            continue
        strokes.append(
            Stroke(
                left.index,
                right.index,
                direction,
                high=max(left.price, right.price),
                low=min(left.price, right.price),
            )
        )
    return strokes
