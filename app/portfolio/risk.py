from dataclasses import replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Mapping

from .models import PortfolioSnapshot


DEFAULT_THRESHOLDS = {
    "long_term_core": (Decimal("0.30"), Decimal("0.35")),
    "active_equity": (Decimal("0.25"), Decimal("0.30")),
}

RATIO = Decimal("0.0001")


def _threshold(value) -> Decimal:
    text = str(value).strip()
    try:
        if text.endswith("%"):
            result = Decimal(text[:-1].strip()) / Decimal("100")
        else:
            result = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("concentration thresholds must be numeric") from None
    if not result.is_finite():
        raise ValueError("concentration thresholds must be finite")
    return result


def _thresholds(strategy_config):
    thresholds = dict(DEFAULT_THRESHOLDS)
    if strategy_config is None:
        return thresholds
    if not isinstance(strategy_config, Mapping):
        raise ValueError("strategy config must be a mapping")
    accounts = strategy_config.get("accounts", {})
    if not isinstance(accounts, Mapping):
        raise ValueError("strategy accounts must be a mapping")
    for strategy, values in accounts.items():
        if not isinstance(values, Mapping):
            raise ValueError(f"strategy thresholds for {strategy} must be a mapping")
        warning = values.get("concentration_warning")
        high = values.get("concentration_high")
        if warning is None or high is None:
            raise ValueError(
                f"strategy thresholds for {strategy} require concentration_warning "
                "and concentration_high"
            )
        parsed_warning = _threshold(warning)
        parsed_high = _threshold(high)
        if not Decimal("0") <= parsed_warning <= parsed_high <= Decimal("1"):
            raise ValueError(
                f"strategy thresholds for {strategy} must satisfy "
                "0 <= warning <= high <= 1"
            )
        thresholds[strategy] = (parsed_warning, parsed_high)
    return thresholds


def _ratio(value):
    return value.quantize(RATIO, rounding=ROUND_HALF_UP)


def apply_portfolio_risk(snapshot: PortfolioSnapshot, strategy_config=None):
    """Return a new snapshot with holding warnings and combined exposure ratios."""
    thresholds = _thresholds(strategy_config)
    sector_values = {}
    market_values = {}
    accounts = []

    for account_snapshot in snapshot.accounts:
        warning_limit, high_limit = thresholds.get(
            account_snapshot.account.strategy, (None, None)
        )
        holdings = []
        for holding_snapshot in account_snapshot.holdings:
            warnings = list(holding_snapshot.warnings)
            if warning_limit is not None:
                weight = holding_snapshot.account_weight
                if weight >= high_limit and "高集中度风险" not in warnings:
                    warnings.append("高集中度风险")
                elif (
                    (
                        weight >= warning_limit
                        if account_snapshot.account.strategy == "long_term_core"
                        else weight > warning_limit
                    )
                    and "集中度预警" not in warnings
                ):
                    warnings.append("集中度预警")
            holdings.append(replace(holding_snapshot, warnings=tuple(warnings)))
            sector = holding_snapshot.holding.sector
            market = holding_snapshot.holding.market
            sector_values[sector] = sector_values.get(sector, Decimal("0")) + holding_snapshot.market_value
            market_values[market] = market_values.get(market, Decimal("0")) + holding_snapshot.market_value
        accounts.append(replace(account_snapshot, holdings=tuple(holdings)))

    total_assets = snapshot.total_assets
    exposure_by_sector = {
        key: _ratio(value / total_assets) for key, value in sector_values.items()
    }
    exposure_by_market = {
        key: _ratio(value / total_assets) for key, value in market_values.items()
    }
    return replace(
        snapshot,
        accounts=tuple(accounts),
        exposure_by_sector=exposure_by_sector,
        exposure_by_market=exposure_by_market,
    )
