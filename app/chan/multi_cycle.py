"""Multi timeframe Chan analysis configuration."""

CYCLES = {
    "weekly": "position",
    "daily": "direction",
    "120m": "core_buy_point",
    "30m": "pullback_confirmation",
    "5m": "price_optimization",
    "15m": "execution",
}


def cycle_role(cycle):
    return CYCLES.get(cycle, "unknown")
