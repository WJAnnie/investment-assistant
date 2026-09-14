from decimal import Decimal, ROUND_HALF_UP

from .models import (
    AccountSnapshot,
    HoldingSnapshot,
    PortfolioConfig,
    PortfolioSnapshot,
)


MONEY = Decimal("0.01")
RATIO = Decimal("0.0001")


def _money(value):
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _ratio(value):
    return value.quantize(RATIO, rounding=ROUND_HALF_UP)


def build_account_snapshot(account, valuations):
    rows = []
    for holding in account.holdings:
        valuation = valuations.get(holding.code)
        usable = (
            valuation
            if valuation is not None
            and valuation.freshness == "fresh"
            and valuation.price > 0
            else None
        )
        current_price = usable.price if usable else holding.baseline_price
        if usable is not None and holding.quantity is not None:
            market_value = _money(holding.quantity * current_price)
            monetary_profit = _money(holding.quantity * (current_price - holding.cost_price))
            precision = "exact"
        else:
            market_value = _money(holding.baseline_value)
            monetary_profit = None
            precision = "baseline"
        price_return = (
            _ratio(current_price / holding.cost_price - Decimal("1"))
            if usable is not None and current_price is not None and holding.cost_price > 0
            else None
        )
        rows.append((holding, valuation, current_price, market_value, monetary_profit,
                     precision, price_return))

    holdings_value = _money(sum((row[3] for row in rows), Decimal("0")))
    total_assets = _money(account.cash + holdings_value)
    snapshots = tuple(
        HoldingSnapshot(
            account_id=account.account_id,
            holding=row[0],
            current_price=row[2],
            market_value=row[3],
            account_weight=_ratio(row[3] / total_assets),
            return_from_cost=row[6],
            monetary_profit=row[4],
            value_precision=row[5],
            valuation=row[1],
        )
        for row in rows
    )
    return AccountSnapshot(
        account=account,
        holdings=snapshots,
        holdings_value=holdings_value,
        cash=_money(account.cash),
        total_assets=total_assets,
        position_percent=_ratio(holdings_value / total_assets),
    )


def build_portfolio_snapshot(config: PortfolioConfig, valuations):
    if config.schema_version < 2:
        raise ValueError("multi-account reports require schema_version 2")
    accounts = tuple(build_account_snapshot(account, valuations) for account in config.accounts)
    total_assets = _money(sum((item.total_assets for item in accounts), Decimal("0")))
    holdings_value = _money(sum((item.holdings_value for item in accounts), Decimal("0")))
    cash = _money(sum((item.cash for item in accounts), Decimal("0")))
    return PortfolioSnapshot(
        accounts=accounts,
        total_assets=total_assets,
        holdings_value=holdings_value,
        cash=cash,
        position_percent=_ratio(holdings_value / total_assets),
    )
