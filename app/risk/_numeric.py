"""Shared numeric coercion helpers for risk calculations."""

from decimal import Decimal, InvalidOperation


def to_decimal(value, name: str) -> Decimal:
    """Coerce numbers or numeric strings to a finite Decimal value."""
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value).strip())
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError(f"{name} 必须是数值") from None
    if not result.is_finite():
        raise ValueError(f"{name} 必须是有限数值")
    return result
