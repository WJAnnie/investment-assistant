from dataclasses import dataclass


@dataclass
class Quote:
    code: str
    name: str
    price: float
    change: float
    timestamp: str
    source: str = ""
    market_time: str = ""
