import math
from collections.abc import Mapping

from .models import Quote


def validate_quote(data) -> bool:
    if isinstance(data, Quote):
        values = data.__dict__
    elif isinstance(data, Mapping):
        values = data
    else:
        return False

    required = ("code", "name", "price", "change", "timestamp")
    if any(key not in values for key in required):
        return False

    if not isinstance(values["code"], str) or not values["code"].strip():
        return False
    if not isinstance(values["name"], str) or not values["name"].strip():
        return False
    if not isinstance(values["timestamp"], str) or not values["timestamp"].strip():
        return False

    try:
        price = float(values["price"])
        change = float(values["change"])
    except (TypeError, ValueError):
        return False

    return math.isfinite(price) and price >= 0 and math.isfinite(change)
