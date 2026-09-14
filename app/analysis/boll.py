from .indicator import sma


def calculate_boll(close, period=20, multiplier=2):
    middle = sma(close, period)
    upper = []
    lower = []

    for i, m in enumerate(middle):
        window = close[max(0, i-period+1):i+1]
        variance = sum((x-m)**2 for x in window) / len(window)
        std = variance ** 0.5
        upper.append(m + multiplier * std)
        lower.append(m - multiplier * std)

    return {
        "upper": upper,
        "middle": middle,
        "lower": lower,
    }
