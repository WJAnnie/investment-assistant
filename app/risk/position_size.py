"""仓位计算——理论仓位 = 单笔最大亏损 ÷ 每股风险，最终仓位 = min(理论仓位, 各上限)。"""

from dataclasses import dataclass
from decimal import Decimal

from ._numeric import to_decimal

try:
    from collections.abc import Mapping
except ImportError:  # Python < 3.9 fallback
    from typing import Mapping


@dataclass(frozen=True)
class PositionSize:
    theoretical_shares: Decimal
    final_shares: Decimal
    position_percent: Decimal
    capped_by: tuple


def calculate_theoretical_shares(risk_amount, per_share_risk) -> Decimal:
    """理论仓位（股数）= 单笔最大亏损 ÷ 每股风险；输入非正数显式拒绝。"""
    amount = to_decimal(risk_amount, "单笔最大亏损")
    risk = to_decimal(per_share_risk, "每股风险")
    if amount <= 0:
        raise ValueError("单笔最大亏损必须为正数")
    if risk <= 0:
        raise ValueError("每股风险必须为正数")
    return amount / risk


def _cap_items(position_caps):
    if position_caps is None:
        return ()
    if isinstance(position_caps, Mapping):
        return tuple((str(name), cap) for name, cap in position_caps.items())
    return tuple((str(name), cap) for name, cap in position_caps)


def calculate_position_size(
    risk_amount,
    entry,
    invalidation,
    account_assets,
    position_caps=(),
) -> PositionSize:
    """由风险预算推导新开仓仓位，并用各仓位百分比上限裁剪。

    每股风险为 0 或负数（失效点不低于入场价）时显式拒绝。
    """
    entry = to_decimal(entry, "入场价")
    invalidation = getattr(invalidation, "price", invalidation)
    stop = to_decimal(invalidation, "失效点价格")
    assets = to_decimal(account_assets, "账户资产")
    if entry <= 0 or stop <= 0 or assets <= 0:
        raise ValueError("入场价、失效点价格与账户资产都必须为正数")

    theoretical_shares = calculate_theoretical_shares(risk_amount, entry - stop)
    theoretical_percent = theoretical_shares * entry / assets

    caps = []
    for name, cap in _cap_items(position_caps):
        cap_value = to_decimal(cap, f"仓位上限 {name}")
        if cap_value < 0:
            raise ValueError(f"仓位上限 {name} 不能为负数")
        caps.append((name, cap_value))
    # 内建 100% 上限：即使调用方省略 caps，也不允许输出杠杆仓位（M3）。
    final_percent = min(
        [theoretical_percent, Decimal("1"), *(value for _name, value in caps)]
    )
    capped_by = tuple(
        name
        for name, value in caps
        if value <= theoretical_percent and value == final_percent
    )
    final_shares = final_percent * assets / entry
    return PositionSize(
        theoretical_shares=theoretical_shares,
        final_shares=final_shares,
        position_percent=final_percent,
        capped_by=capped_by,
    )
