def calculate_rsi(close, period=6):
    if not isinstance(period, int) or isinstance(period, bool) or period <= 0:
        raise ValueError("period must be a positive integer")
    if len(close) < 2:
        return []

    gains = []
    losses = []

    for i in range(1, len(close)):
        diff = close[i] - close[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    result = []
    for i in range(len(gains)):
        g = sum(gains[max(0, i-period+1):i+1]) / period
        l = sum(losses[max(0, i-period+1):i+1]) / period
        if l == 0:
            result.append(100 if g > 0 else 50)
        else:
            rs = g / l
            result.append(100 - 100 / (1 + rs))

    return result
