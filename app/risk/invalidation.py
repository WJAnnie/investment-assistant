"""失效点（invalidation point）模型。"""

from dataclasses import dataclass
from decimal import Decimal

from ._numeric import to_decimal


@dataclass(frozen=True)
class InvalidationPoint:
    """价格失效点：跌破该价位即入场逻辑失效。

    该价位是每股风险（entry - invalidation）与盈亏比计算（R12）的
    分母来源，必须显式提供，不允许默认或伪造。
    """

    price: Decimal
    reason: str = ""
    source: str = ""

    def __post_init__(self):
        price = to_decimal(self.price, "失效点价格")
        if price <= 0:
            raise ValueError("失效点价格必须为正数")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "reason", str(self.reason))
        object.__setattr__(self, "source", str(self.source))
