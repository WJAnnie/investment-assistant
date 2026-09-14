"""Timeframe and cycle-role contract for the multi-cycle trading system.

Rule references: R03 (cycle hierarchy), R05 (cycle responsibilities).
"""

from enum import Enum


class CycleRole(Enum):
    """Responsibility of each timeframe within the decision pipeline."""

    WEEKLY_POSITION = "weekly_position"
    DAILY_DIRECTION = "daily_direction"
    CORE_BUY_POINT = "core_buy_point"
    PULLBACK_CONFIRMATION = "pullback_confirmation"
    EXECUTION = "execution"
    PRICE_OPTIMIZATION = "price_optimization"


class Timeframe(Enum):
    """Canonical timeframes, ordered from high to low level.

    Lower timeframes may only optimize price or trigger execution timing
    (R03); they can never turn a WAIT decision into BUY on their own (R06).
    """

    WEEKLY = "weekly"
    DAILY = "daily"
    MIN_120 = "120m"
    MIN_30 = "30m"
    MIN_15 = "15m"
    MIN_5 = "5m"


TIMEFRAME_ROLES = {
    Timeframe.WEEKLY: CycleRole.WEEKLY_POSITION,
    Timeframe.DAILY: CycleRole.DAILY_DIRECTION,
    Timeframe.MIN_120: CycleRole.CORE_BUY_POINT,
    Timeframe.MIN_30: CycleRole.PULLBACK_CONFIRMATION,
    Timeframe.MIN_15: CycleRole.EXECUTION,
    Timeframe.MIN_5: CycleRole.PRICE_OPTIMIZATION,
}


def cycle_role(timeframe: Timeframe) -> CycleRole:
    """Return the CycleRole bound to a Timeframe (R05)."""
    return TIMEFRAME_ROLES[timeframe]
