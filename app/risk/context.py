"""决策引擎使用的风险上下文。"""

from dataclasses import dataclass
from decimal import Decimal

from ._numeric import to_decimal
from .invalidation import InvalidationPoint


@dataclass(frozen=True)
class RiskContext:
    """build_decision 计算仓位所需的最小风险输入。

    缺少该上下文时，决策引擎不计算仓位（研究参考模式），
    绝不使用固定映射伪造仓位数字（R08/R11）。
    """

    account_assets: Decimal
    entry: Decimal
    target: Decimal
    invalidation: InvalidationPoint
    risk_ratio: Decimal = None

    def __post_init__(self):
        assets = to_decimal(self.account_assets, "账户资产")
        entry = to_decimal(self.entry, "入场价")
        target = to_decimal(self.target, "目标价")
        if assets <= 0 or entry <= 0 or target <= 0:
            raise ValueError("账户资产、入场价与目标价都必须为正数")
        invalidation = self.invalidation
        if isinstance(invalidation, dict):
            invalidation = InvalidationPoint(**invalidation)
        elif not isinstance(invalidation, InvalidationPoint):
            invalidation = InvalidationPoint(invalidation)
        ratio = self.risk_ratio
        if ratio is not None:
            if isinstance(ratio, str) and ratio.strip().endswith("%"):
                ratio = to_decimal(ratio.strip()[:-1], "风险比例") / Decimal("100")
            else:
                ratio = to_decimal(ratio, "风险比例")
        object.__setattr__(self, "account_assets", assets)
        object.__setattr__(self, "entry", entry)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "invalidation", invalidation)
        object.__setattr__(self, "risk_ratio", ratio)
