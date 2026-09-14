"""盈亏比（reward-to-risk ratio）——R12/R13。"""

from decimal import Decimal

try:
    from collections.abc import Mapping
except ImportError:  # Python < 3.9 fallback
    from typing import Mapping

from ._numeric import to_decimal
from .budget import load_risk_rules

MINIMUM_RR = Decimal("2.0")  # R12：RR < 2.0 拒绝新开仓
PREFERRED_RR = Decimal("2.5")  # R13：优选盈亏比


def rr_thresholds(rules=None) -> tuple[Decimal, Decimal]:
    """Return validated (minimum, preferred) RR thresholds.

    Missing or unreadable configuration falls back to the rule defaults.
    Explicit malformed values fail closed so the decision engine can block
    opening a position instead of silently weakening the risk rule.
    """
    if rules is None:
        try:
            rules = load_risk_rules()
        except Exception:  # defensive boundary for injected/custom loaders
            rules = {}
    if not isinstance(rules, Mapping):
        raise ValueError("rules 必须是映射类型")
    section = rules.get("risk")
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ValueError("rules 的 risk 段必须是映射类型")
    minimum = to_decimal(section.get("minimum_rr", MINIMUM_RR), "最低盈亏比")
    preferred = to_decimal(section.get("preferred_rr", PREFERRED_RR), "优选盈亏比")
    if minimum <= 0 or preferred <= 0:
        raise ValueError("盈亏比阈值必须为正数")
    if preferred < minimum:
        raise ValueError("优选盈亏比不能低于最低盈亏比")
    return minimum, preferred


def calculate_rr(entry, target, invalidation) -> Decimal:
    """计算多头盈亏比 =（target - entry）÷（entry - invalidation）。

    每股风险为 0 或负数（失效点不在入场价下方）时显式拒绝；
    目标价不高于入场价同样拒绝，避免输出无意义的负盈亏比。
    """
    entry = to_decimal(entry, "入场价")
    target = to_decimal(target, "目标价")
    invalidation = getattr(invalidation, "price", invalidation)
    invalidation = to_decimal(invalidation, "失效点价格")
    if entry <= 0 or target <= 0 or invalidation <= 0:
        raise ValueError("入场价、目标价与失效点价格都必须为正数")

    per_share_risk = entry - invalidation
    if per_share_risk <= 0:
        raise ValueError("每股风险必须为正数（失效点必须低于入场价）")
    reward = target - entry
    if reward <= 0:
        raise ValueError("目标价必须高于入场价")
    return reward / per_share_risk
