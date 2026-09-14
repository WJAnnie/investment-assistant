from datetime import datetime

from app.market.models import Quote


DEFAULT_MARKET_CODES = (
    "SH.000001",
    "SZ.399001",
    "SZ.399006",
    "SH.000688",
    "SH.000905",
    "SH.000300",
    "SH.000016",
    "SZ.399005",
)


def run_market_snapshot(
    codes,
    collector,
    notifier=None,
    now=None,
    title="临时上午行情简报",
    report_kind="snapshot",
):
    """Collect a lightweight realtime market snapshot without historical K-lines."""
    if collector is None:
        raise ValueError("collector is required")

    symbols = list(codes or [])
    if not symbols or any(not isinstance(code, str) or not code.strip() for code in symbols):
        raise ValueError("codes must contain at least one non-empty string")

    quotes = []
    errors = []
    for code in symbols:
        try:
            quote = collector.get_quote(code)
        except Exception as exc:
            errors.append(f"{code}: {exc}")
            continue
        if not isinstance(quote, Quote):
            errors.append(f"{code}: invalid quote")
            continue
        quotes.append(quote)

    report = format_market_snapshot(
        quotes,
        errors,
        now=now,
        title=title,
        report_kind=report_kind,
    )
    notified = False
    if notifier is not None:
        try:
            notified = bool(notifier.send(report))
        except Exception:
            notified = False

    return {
        "status": "completed" if not errors else "partial",
        "total": len(symbols),
        "succeeded": len(quotes),
        "failed": len(errors),
        "quotes": quotes,
        "report": report,
        "notified": notified,
        "errors": errors,
    }


def format_market_snapshot(
    quotes,
    errors=None,
    now=None,
    title="临时上午行情简报",
    report_kind="snapshot",
):
    current_time = now or datetime.now().astimezone()
    lines = [
        f"📊 {title}",
        f"时间：{current_time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "说明：A股公开实时指数快照；港股暂不纳入。",
        "",
    ]
    lines.extend(
        f"{quote.name}：{quote.price:.4f}（{quote.change:+.2f}%）"
        for quote in quotes
    )
    stage_lines = _format_stage_lines(quotes, report_kind)
    if stage_lines:
        lines.extend(["", *stage_lines])
    if errors:
        lines.extend(["", "⚠️ 数据源异常：", *errors])
    if quotes:
        changes = [quote.change for quote in quotes]
        if all(change < 0 for change in changes):
            conclusion = "主要观察指数全线走弱，盘中风险偏好偏低；不等同于收盘结论。"
        elif all(change > 0 for change in changes):
            conclusion = "主要观察指数整体走强，盘中风险偏好改善；不等同于收盘结论。"
        else:
            conclusion = "指数涨跌分化，盘面仍需观察强弱方向；不等同于收盘结论。"
        lines.extend(
            [
                "",
                f"盘面判断：{conclusion}",
                "操作纪律：不因盘中波动直接补仓或追涨，等待日线/120分钟结构确认。",
                "数据源：Sina public quote endpoint",
            ]
        )
    else:
        lines.extend(["", "盘面判断：本次没有取得有效指数行情。"])
    return "\n".join(lines)


def _format_stage_lines(quotes, report_kind):
    if not quotes or report_kind == "snapshot":
        return []

    strongest = max(quotes, key=lambda quote: quote.change)
    weakest = min(quotes, key=lambda quote: quote.change)
    average_change = sum(quote.change for quote in quotes) / len(quotes)

    if report_kind == "global":
        return [
            "盘前扫描：当前仅覆盖 A 股指数，海外市场和港股暂未纳入。",
            "使用方式：作为开盘前参考，不把隔夜或盘前波动直接当作交易信号。",
        ]
    if report_kind == "morning":
        return [
            f"早盘观察：相对最强为{strongest.name}（{strongest.change:+.2f}%），"
            f"相对最弱为{weakest.name}（{weakest.change:+.2f}%）。",
            "今日重点：观察开盘后的量价是否修复，不在第一小时追涨。",
        ]
    if report_kind == "midday":
        return [
            f"午盘强弱：最强{strongest.name}（{strongest.change:+.2f}%），"
            f"最弱{weakest.name}（{weakest.change:+.2f}%）。",
            f"风格观察：观察指数平均涨跌幅为{average_change:+.2f}%，"
            "下午关注跌幅是否收窄或继续扩散。",
        ]
    if report_kind == "trading":
        return [
            "午后执行：先检查持仓仓位、止损和计划，不因盘中波动自动下单。",
            f"盘中观察：重点跟踪{strongest.name}与{weakest.name}的强弱变化，"
            "等待结构确认后再决定是否行动。",
        ]
    if report_kind == "closing":
        return [
            "收盘复盘：记录今日指数强弱排序、收盘位置和次日观察条件。",
            f"今日强弱：{strongest.name}相对抗跌，{weakest.name}相对承压；"
            f"观察指数平均涨跌幅为{average_change:+.2f}%。",
        ]
    return []
