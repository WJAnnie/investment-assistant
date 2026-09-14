"""MACD indicator aligned to trading-rule system V8.1 (R03)."""

from .indicator import ema


FAST_PERIOD = 6
SLOW_PERIOD = 13
SIGNAL_PERIOD = 4


def calculate_macd(close):
    """Compute MACD with the R03 parameter set (6/13/4).

    The signature stays `calculate_macd(close)` for compatibility; the
    fast/slow/signal periods are intentionally fixed here so callers cannot
    silently drift back to the legacy 12/26/9 defaults.
    """
    dif = []
    ema_fast = ema(close, FAST_PERIOD)
    ema_slow = ema(close, SLOW_PERIOD)

    for a, b in zip(ema_fast, ema_slow):
        dif.append(a - b)

    dea = ema(dif, SIGNAL_PERIOD)
    hist = [a - b for a, b in zip(dif, dea)]

    return {
        "dif": dif,
        "dea": dea,
        "hist": hist,
    }
