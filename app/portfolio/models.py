from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class HoldingConfig:
    code: str
    name: str
    market: str
    instrument_type: str
    valuation_mode: str
    cost_price: Decimal
    baseline_value: Decimal
    sector: str
    theme: str
    quantity: Decimal | None = None
    position_percent: Decimal | None = None
    baseline_price: Decimal | None = None


@dataclass(frozen=True)
class AccountConfig:
    account_id: str
    name: str
    strategy: str
    baseline_date: date
    total_assets: Decimal
    cash: Decimal
    holdings: tuple[HoldingConfig, ...]

    def __post_init__(self):
        object.__setattr__(self, "holdings", tuple(self.holdings))


@dataclass(frozen=True)
class PortfolioConfig:
    schema_version: int
    accounts: tuple[AccountConfig, ...] = ()
    legacy_holdings: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "accounts", tuple(self.accounts))
        object.__setattr__(self, "legacy_holdings", tuple(_freeze(entry) for entry in self.legacy_holdings))


@dataclass(frozen=True)
class Valuation:
    code: str
    price: Decimal
    change_percent: Decimal
    as_of: datetime
    source: str
    freshness: str = "fresh"
    error: str | None = None


@dataclass(frozen=True)
class HoldingSnapshot:
    account_id: str
    holding: HoldingConfig
    current_price: Decimal | None
    market_value: Decimal
    account_weight: Decimal
    return_from_cost: Decimal | None
    monetary_profit: Decimal | None
    value_precision: str
    valuation: Valuation | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True)
class AccountSnapshot:
    account: AccountConfig
    holdings: tuple[HoldingSnapshot, ...]
    holdings_value: Decimal
    cash: Decimal
    total_assets: Decimal
    position_percent: Decimal
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "holdings", tuple(self.holdings))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True)
class PortfolioSnapshot:
    accounts: tuple[AccountSnapshot, ...]
    total_assets: Decimal
    holdings_value: Decimal
    cash: Decimal
    position_percent: Decimal
    exposure_by_sector: Mapping[str, Decimal] = field(default_factory=dict)
    exposure_by_market: Mapping[str, Decimal] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "accounts", tuple(self.accounts))
        object.__setattr__(self, "exposure_by_sector", MappingProxyType(dict(self.exposure_by_sector)))
        object.__setattr__(self, "exposure_by_market", MappingProxyType(dict(self.exposure_by_market)))
        object.__setattr__(self, "warnings", tuple(self.warnings))
