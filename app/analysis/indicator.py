"""Small, dependency-free technical indicator helpers."""


def _validate_period(period):
    if not isinstance(period, int) or isinstance(period, bool) or period <= 0:
        raise ValueError("period must be a positive integer")


def ema(values, period):
    _validate_period(period)
    if not values:
        return []

    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def sma(values, period):
    _validate_period(period)
    result = []
    for i in range(len(values)):
        window = values[max(0, i - period + 1):i + 1]
        result.append(sum(window) / len(window))
    return result
