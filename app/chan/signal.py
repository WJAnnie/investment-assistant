from enum import Enum


class ChanSignal(Enum):
    WAIT = "WAIT"
    FIRST_BUY = "FIRST_BUY"
    CLASS_SECOND_BUY = "CLASS_SECOND_BUY"
    SECOND_BUY = "SECOND_BUY"
    THIRD_BUY = "THIRD_BUY"
    SELL_RISK = "SELL_RISK"


class ChanSignalEngine:
    def evaluate(self, context: dict) -> ChanSignal:
        """Return a research signal, applying risk and confirmation gates first."""
        if self._flag(context.get("risk_blocked")) or self._flag(context.get("sell_risk")):
            return ChanSignal.SELL_RISK

        if (
            self._flag(context.get("first_buy"))
            and self._flag(context.get("divergence"))
            and self._flag(context.get("trend_confirm"))
            and self._flag(context.get("multi_cycle_confirm"))
        ):
            return ChanSignal.FIRST_BUY

        if (
            self._flag(context.get("third_buy"))
            and self._flag(context.get("zhongshu_breakout"))
            and self._flag(context.get("trend_confirm"))
            and self._flag(context.get("multi_cycle_confirm"))
        ):
            return ChanSignal.THIRD_BUY

        if (
            self._flag(context.get("second_buy"))
            and self._flag(context.get("zhongshu"))
            and self._flag(context.get("trend_confirm"))
            and self._flag(context.get("multi_cycle_confirm"))
        ):
            return ChanSignal.SECOND_BUY

        if (
            self._flag(context.get("class_second"))
            and self._flag(context.get("divergence"))
            and self._flag(context.get("trend_confirm"))
            and self._flag(context.get("multi_cycle_confirm"))
        ):
            return ChanSignal.CLASS_SECOND_BUY

        return ChanSignal.WAIT

    @staticmethod
    def _flag(value):
        detected = getattr(value, "detected", None)
        return detected if detected is not None else bool(value)
