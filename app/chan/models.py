from dataclasses import dataclass
from typing import List


@dataclass
class KLine:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0


@dataclass
class FenXing:
    index: int
    price: float
    kind: str  # top / bottom


@dataclass
class Bi:
    start: int
    end: int
    direction: str  # up / down
    high: float
    low: float


@dataclass
class ZhongShu:
    start: int
    end: int
    high: float
    low: float


@dataclass
class ChanSignal:
    level: str
    signal: str
    score: int
    reason: List[str]
