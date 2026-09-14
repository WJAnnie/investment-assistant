from app.chan.signal import ChanSignal
from app.market.models import Quote


def format_daily_report(quote: Quote, technical: dict, signal: ChanSignal, decision) -> str:
    score = technical["score"]
    return "\n".join(
        (
            "📊 Investment Assistant 日报",
            f"标的：{quote.name} ({quote.code})",
            f"价格：{quote.price:.2f}，涨跌：{quote.change:.2f}%",
            f"技术评分：{score}",
            f"缠论信号：{signal.value}",
            f"建议动作：{decision.action}",
            "纪律：仅供研究复核，不自动交易，不构成投资建议。",
        )
    )
