from .models import ChanSignal
from .signal import ChanSignal as SignalType, ChanSignalEngine


def classify_buy_point(level: str, context: dict) -> ChanSignal:
    """缠论买点分类基础。

    注意：该版本只建立结构，不自动交易。
    后续接入完整中枢、背驰和多周期共振。
    """

    reasons = []

    if context.get("divergence"):
        reasons.append("存在背驰辅助信号")

    if context.get("trend_confirm"):
        reasons.append("趋势确认")

    if context.get("zhongshu"):
        reasons.append("中枢结构存在")

    signal = ChanSignalEngine().evaluate(context)
    scores = {
        SignalType.FIRST_BUY: 80,
        SignalType.CLASS_SECOND_BUY: 75,
        SignalType.SECOND_BUY: 85,
        SignalType.THIRD_BUY: 90,
        SignalType.SELL_RISK: 20,
        SignalType.WAIT: 50,
    }
    return ChanSignal(level, signal.value, scores[signal], reasons)
