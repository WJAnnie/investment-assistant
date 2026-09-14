from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import yaml

from .models import AccountConfig, HoldingConfig, PortfolioConfig


class _DecimalSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that keeps decimal scalar text exact."""


def _construct_decimal(loader: yaml.SafeLoader, node: yaml.nodes.ScalarNode) -> Decimal:
    return Decimal(loader.construct_scalar(node))


_DecimalSafeLoader.add_constructor("tag:yaml.org,2002:float", _construct_decimal)


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field_name} must be numeric") from None
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _required_string(data: Mapping[str, Any], field_name: str, context: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{field_name} must be a non-empty string")
    return value.strip()


def _optional_decimal(data: Mapping[str, Any], field_name: str) -> Decimal | None:
    if data.get(field_name) is None:
        return None
    return _decimal(data[field_name], field_name)


def parse_portfolio(data: Any, allow_unbalanced: bool = False) -> PortfolioConfig:
    """Parse and validate an account-first portfolio or explicit legacy payload."""
    if not isinstance(data, dict):
        raise ValueError("portfolio config must be a mapping")

    declared_version = data.get("schema_version")
    if declared_version is not None and (
        isinstance(declared_version, bool) or not isinstance(declared_version, int)
    ):
        raise ValueError("schema_version must be an integer")
    if declared_version not in (None, 1, 2):
        raise ValueError(f"unsupported schema_version: {declared_version}")
    if declared_version == 2 and "accounts" not in data:
        raise ValueError("schema_version 2 requires accounts")

    if "accounts" not in data:
        legacy = data.get("portfolio")
        if declared_version == 2 or not isinstance(legacy, list):
            raise ValueError("legacy portfolio must be an explicit list")
        return PortfolioConfig(schema_version=1, legacy_holdings=tuple(legacy))
    if declared_version is None:
        raise ValueError("account configs require schema_version 2")
    if declared_version not in (None, 2):
        raise ValueError("account configs require schema_version 2")
    if not isinstance(data["accounts"], list):
        raise ValueError("accounts must be a list")

    account_ids: set[str] = set()
    accounts: list[AccountConfig] = []
    for index, raw_value in enumerate(data["accounts"]):
        raw_account = _mapping(raw_value, f"account[{index}]")
        account_context = f"account[{index}]"
        account_id = _required_string(raw_account, "account_id", account_context)
        if account_id in account_ids:
            raise ValueError(f"duplicate account_id: {account_id}")
        account_ids.add(account_id)
        raw_holdings = raw_account.get("holdings", [])
        if not isinstance(raw_holdings, list):
            raise ValueError(f"holdings must be a list for account {account_id}")
        seen_codes: set[str] = set()
        holdings: list[HoldingConfig] = []
        for holding_index, raw_value in enumerate(raw_holdings):
            raw = _mapping(raw_value, f"holding[{holding_index}]")
            holding_context = f"account {account_id} holding[{holding_index}]"
            code = _required_string(raw, "code", holding_context)
            if code in seen_codes:
                raise ValueError(f"duplicate holding code {code} in account {account_id}")
            seen_codes.add(code)
            position = _optional_decimal(raw, "position_percent")
            if position is not None and not Decimal("0") <= position <= Decimal("1"):
                raise ValueError("position_percent must be between 0 and 1")
            quantity = _optional_decimal(raw, "quantity")
            if quantity is not None and quantity <= 0:
                raise ValueError("quantity must be positive")
            cost_price = _decimal(raw.get("cost_price"), "cost_price")
            if cost_price <= 0:
                raise ValueError("cost_price must be positive")
            baseline_price = _optional_decimal(raw, "baseline_price")
            if baseline_price is not None and baseline_price <= 0:
                raise ValueError("baseline_price must be positive")
            baseline_value = _decimal(raw.get("baseline_value"), "baseline_value")
            if baseline_value <= 0:
                raise ValueError("baseline_value must be positive")
            holdings.append(HoldingConfig(
                code=code,
                name=_required_string(raw, "name", holding_context),
                market=_required_string(raw, "market", holding_context),
                instrument_type=_required_string(raw, "instrument_type", holding_context),
                valuation_mode=_required_string(raw, "valuation_mode", holding_context),
                cost_price=cost_price,
                baseline_value=baseline_value,
                sector=_required_string(raw, "sector", holding_context),
                theme=_required_string(raw, "theme", holding_context),
                quantity=quantity,
                position_percent=position,
                baseline_price=baseline_price,
            ))
        total_assets = _decimal(raw_account.get("total_assets"), "total_assets")
        cash = _decimal(raw_account.get("cash"), "cash")
        if total_assets <= 0:
            raise ValueError("total_assets must be positive")
        if cash < 0:
            raise ValueError("cash must be non-negative")
        baseline_sum = cash + sum((h.baseline_value for h in holdings), Decimal("0"))
        if not allow_unbalanced and abs(baseline_sum - total_assets) > Decimal("0.01"):
            raise ValueError(f"account {account_id} does not reconcile")
        baseline_date = _required_string(raw_account, "baseline_date", account_context)
        try:
            parsed_date = date.fromisoformat(baseline_date)
        except ValueError:
            raise ValueError(f"{account_context}.baseline_date must be an ISO date") from None
        accounts.append(AccountConfig(
            account_id=account_id,
            name=_required_string(raw_account, "name", account_context),
            strategy=_required_string(raw_account, "strategy", account_context),
            baseline_date=parsed_date,
            total_assets=total_assets,
            cash=cash,
            holdings=tuple(holdings),
        ))
    return PortfolioConfig(schema_version=2, accounts=tuple(accounts))


def load_portfolio(path: str | Path | None = None) -> PortfolioConfig:
    """Load a portfolio YAML file, defaulting to the production configuration."""
    config_path = Path(path) if path else Path(__file__).resolve().parents[2] / "config" / "portfolio.yaml"
    with config_path.open("r", encoding="utf-8") as handle:
        return parse_portfolio(yaml.load(handle, Loader=_DecimalSafeLoader))
