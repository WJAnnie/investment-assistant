"""Stable JSON conversion helpers shared by CLI and integrations."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


def to_jsonable(value):
    """Convert project values to plain JSON-compatible containers."""
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value):
        # Field-wise recursion supports read-only MappingProxyType values without
        # the deepcopy performed by dataclasses.asdict.
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    return value
