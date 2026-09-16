"""Deterministic, offline renderers for scheduled portfolio reports."""

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
import math

from app.portfolio.global_market import format_overnight_lines
from app.utils.redaction import redact_secrets


STAGE_HEADINGS = {
    "global": "隔夜与全球影响",
    "morning": "开盘前观察",
    "midday": "午盘变化复核",
    "trading": "执行条件复核",
    "closing": "A/H股收盘复盘与恒生科技",
}

STAGE_ACTIONS = {
    "global": (
        "检查隔夜海外市场、港股传导和全球医疗估值日期。",
        "数据缺失、日期错位或市场状态不明时：WAIT，等待补齐后再判断。",
    ),
    "morning": (
        "检查前一交易日收盘条件、开盘缺口、持仓强弱和异常估值。",
        "陈旧/失败数据或缺少开盘证据时：WAIT，不作方向性结论。",
    ),
    "midday": (
        "比较同日晨报参考价变化、持仓异常和数据完整性。",
        "下午尚未收盘或数据不完整时：WAIT，不把半日波动外推为全天结果。",
    ),
    "trading": (
        "检查仓位、集中度，以及已闭合周期的技术/缠论条件。",
        "任一周期未闭合、数据过期、风险上下文缺失或条件不全时：WAIT。",
    ),
    "closing": (
        "检查收盘端点、事前判断配对、基金净值日期和集中度。",
        "基金净值滞后、接口失败或归因不完整时：WAIT，形成次日观察清单。",
    ),
}

_STRATEGIES = {
    "long_term_core": "长期核心配置",
    "active_equity": "主动个股增强",
}


def format_portfolio_report(snapshot, report_kind, now, benchmark=None, analysis=None,
                            stage_context=None, global_market=None):
    analysis_map = _analysis_map(analysis)
    if report_kind not in STAGE_HEADINGS:
        raise ValueError(f"unsupported report_kind: {report_kind}")
    sections = [
        _format_header(report_kind, now),
        _format_action_guidance(report_kind),
        _format_market_context(report_kind, benchmark, analysis, snapshot, global_market=global_market),
        *(
            _format_account(account, report_kind, analysis_map)
            for account in _ordered_accounts(snapshot.accounts)
        ),
        _format_combined(snapshot),
        _format_stage_changes(report_kind, stage_context),
        _format_research_status(analysis, report_kind, global_market=global_market),
        _format_risk(snapshot, report_kind),
        "纪律：仅供研究复核，不自动交易，不构成投资建议。",
    ]
    return "\n\n".join(section for section in sections if section)


def format_failure_reminder(report_kind, now, errors=None, *, allow_retry=True):
    """Render a fail-closed reminder without leaking provider internals."""
    report_kind = report_kind if type(report_kind) is str else None
    heading = STAGE_HEADINGS.get(report_kind, "未知报告阶段")
    timestamp = (
        now.strftime("%Y-%m-%d %H:%M:%S %z")
        if isinstance(now, datetime) and now.utcoffset() is not None
        else "未验证"
    )
    error_count = len(tuple(errors or ()))
    recovery = (
        "本次无法形成完整报告，请检查数据源、时间戳和网络状态，补齐后再重试。"
        if allow_retry is True else
        "本次无法形成有效报告，请检查数据源、时间戳、冻结规则和阶段证据；"
        "本时点不重跑补账，保留失败记录并人工处理。"
    )
    return "\n\n".join((
        f"⚠️ 投资组合｜{heading}\n时间：{timestamp}",
        f"数据未就绪\n{recovery}",
        _format_action_guidance(report_kind),
        f"诊断：已记录 {error_count} 项异常；输出仅保留异常类型，不附带账户或接口内部详情。",
        "纪律：仅供研究复核，不自动下单；任何操作均由本人确认并手动执行。",
    ))


def _format_action_guidance(report_kind):
    check, wait = STAGE_ACTIONS.get(
        report_kind,
        (
            "检查报告类型和调度配置，修正后重新生成报告。",
            "报告阶段无法识别时：WAIT，不形成任何方向性结论。",
        ),
    )
    return (
        "人工操作提醒\n"
        f"1. {check}\n"
        "2. 证据完整且风险条件通过时，也只进入人工复核，不直接形成交易指令。\n"
        f"3. {wait}\n"
        "4. 本系统只做辅助提醒，不自动下单；是否交易由本人确认并手动执行。"
    )


def _format_header(report_kind, now):
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S %z") if isinstance(now, datetime) else str(now)
    return f"📊 投资组合｜{STAGE_HEADINGS[report_kind]}\n时间：{timestamp}"


def _format_market_context(report_kind, benchmark, analysis=None, snapshot=None, global_market=None):
    lines = [f"市场背景：{STAGE_HEADINGS[report_kind]}"]
    if report_kind == "global":
        lines.append("关注隔夜海外风险、港股传导与全球医疗估值的日期差异。")
    elif report_kind == "morning":
        lines.append("核对前一交易日收盘条件：缺口、强弱排序与持仓开盘观察清单。")
    elif report_kind == "midday":
        lines.append("只分析相对晨报基线的变化；半日波动不能代表全天收益。")
    elif report_kind == "trading":
        lines.append("复核仓位、集中度和有效技术条件；没有触发条件就保持观察。")
    elif report_kind == "closing":
        lines.append(
            "核对 A 股收盘价和港股收盘价的精确端点，同时监控恒生科技指数、港股科技敞口和基金净值日期。"
        )
    if isinstance(global_market, dict):
        lines.extend(format_overnight_lines(global_market))
    if benchmark is not None and getattr(benchmark, "freshness", None) != "failed":
        lines.append(
            f"恒生科技指数：{_price(benchmark.price)}（{benchmark.change_percent:+.2f}%），"
            f"数据状态：{_freshness(benchmark)}。"
        )
    elif report_kind == "closing":
        lines.append("恒生科技指数：本次未返回有效点位，不能据此推导港股科技方向。")
    if report_kind == "closing":
        lines.append(_fund_nav_status(snapshot))
    benchmark_analysis = (analysis or {}).get("benchmark") if analysis else None
    if benchmark_analysis:
        lines.append(_format_benchmark_analysis(benchmark_analysis))
    return "\n".join(lines)


def _format_account(account_snapshot, report_kind, analysis_map=None):
    account = account_snapshot.account
    strategy = _STRATEGIES.get(account.strategy, account.strategy)
    lines = [
        f"{account.name}｜{strategy}",
        f"资产：{_money(account_snapshot.total_assets)}，仓位：{_percent(account_snapshot.position_percent)}",
    ]
    holdings = account_snapshot.holdings
    if report_kind == "midday":
        # The delta section below owns price changes. Do not repeat an entire
        # morning analysis or rank cumulative cost P&L as intraday attribution.
        holdings = tuple(
            item for item in holdings if item.warnings or _is_unusable(item)
        )
    for holding in holdings:
        lines.append(_format_holding(holding))
        analysis_item = (analysis_map or {}).get((account.account_id, holding.holding.code))
        if analysis_item is not None and report_kind != "midday":
            lines.append(_format_analysis_item(analysis_item))
            minute_text = _format_minute_context(analysis_item)
            if minute_text:
                lines.append(minute_text)
        if report_kind == "trading":
            lines.append(_format_decision_evidence(analysis_item))
    if report_kind == "midday" and not holdings:
        lines.append("持仓异常：无新增数据/风险警告；价格变化见晨报基线比较。")
    if report_kind == "closing":
        if account.account_id == "A":
            lines.append(_format_hk_exposure(holdings))
    if account_snapshot.warnings:
        lines.append("账户提示：" + "；".join(account_snapshot.warnings))
    return "\n".join(lines)


def _format_holding(item):
    holding = item.holding
    precision = "精确数量估值" if item.value_precision == "exact" else "基准值估算"
    details = [
        f"{holding.code} {holding.name}",
        f"市值 {_money(item.market_value)}",
        f"权重 {_percent(item.account_weight)}",
        precision,
    ]
    if item.return_from_cost is not None:
        details.append(f"成本收益 {_percent(item.return_from_cost)}")
    if item.valuation is not None:
        details.append(f"数据日 {item.valuation.as_of.strftime('%Y-%m-%d')}")
    if item.warnings:
        details.append("警告：" + "、".join(item.warnings))
    if _is_unusable(item):
        details.append("数据已陈旧：" + _freshness(item.valuation))
    return "；".join(details)


def _format_combined(snapshot):
    return (
        f"全部账户｜总资产：{_money(snapshot.total_assets)}\n"
        f"持仓：{_money(snapshot.holdings_value)}，现金：{_money(snapshot.cash)}，"
        f"总仓位：{_percent(snapshot.position_percent)}"
    )


def _ordered_accounts(accounts):
    preferred = {"A": 0, "B": 1}
    return sorted(accounts, key=lambda item: (preferred.get(item.account.account_id, 2), item.account.account_id))


def _is_hk_technology_holding(item):
    holding = item.holding
    return (
        holding.market == "HK"
        and holding.theme in {"hk_technology", "hk_internet"}
    )


def _format_hk_exposure(holdings):
    hk_holdings = tuple(item for item in holdings if _is_hk_technology_holding(item))
    if not hk_holdings:
        return "港股科技敞口：暂无已配置的港股科技/互联网持仓。"
    names = "、".join(f"{item.holding.code} {item.holding.name}" for item in hk_holdings)
    return f"港股科技敞口：{names}；与恒生科技指数联动仅作观察，不替代基金净值核对。"


def _fund_nav_status(snapshot):
    if snapshot is None:
        return "基金净值日期：未提供账户快照，无法核对。"
    entries = []
    for account in snapshot.accounts:
        for item in account.holdings:
            if item.holding.valuation_mode not in {"fund_nav", "qdii_nav"}:
                continue
            if item.valuation is None:
                status = "未返回"
            else:
                status = item.valuation.as_of.strftime("%Y-%m-%d")
            entries.append(f"{item.holding.code} {status}")
    if not entries:
        return "基金净值日期：本次没有可核对的基金/ETF 净值持仓。"
    return "基金净值日期：" + "；".join(entries) + "。"


def _format_stage_changes(report_kind, stage_context):
    if report_kind not in {"midday", "trading", "closing"}:
        return ""
    context = stage_context or {}
    comparison = context.get("comparison") or {}
    lines = ["晨报基线比较（不是开盘以来盈亏，也不是实际交易收益）"]
    if comparison.get("status") not in {"comparable", "partial"}:
        lines.append("无法比较：缺少兼容的同日晨报基线或有效行情，不使用累计成本收益替代。")
    else:
        changed = False
        for item in comparison.get("holdings", ()):
            identity = f"{item.get('account_id', '')}/{item.get('code', '')}"
            if item.get("status") != "comparable":
                lines.append(f"{identity}：无法比较，基线/持仓口径/行情证据不匹配。")
            elif Decimal(item["price_change"]) != 0:
                changed = True
                line = (f"{identity}：参考价 {item['baseline_price']} → {item['current_price']}；"
                        f"价差 {item['price_change']}")
                if item.get("value_change") is not None:
                    line += f"；按不变数量估算市值变化 {_money(item['value_change'])}"
                else:
                    line += "；数量未确认，不推算金额"
                lines.append(line)
        for item in comparison.get("technical_changes", ()):
            changed = True
            lines.append(f"{item['account_id']}/{item['code']}：技术评分 "
                         f"{item['before']} → {item['after']}（不是综合评分）")
        if not changed:
            lines.append("可比较部分未观察到价格/技术分数变化；继续观察，WAIT。")
    if report_kind == "closing":
        lines.append("事前判断 → 收盘结果 → 复核")
        evaluations = (context.get("forecast_evaluation") or {}).get("evaluations", ())
        if not evaluations:
            lines.append("无法评价：没有已事前存档且具备有效收盘端点的明确预测，不补造昨日判断。")
        for item in evaluations:
            target = item.get("target") or {}
            outcome = {"hit": "符合", "miss": "不符合"}.get(item.get("status"), "无法评价")
            lines.append(f"{target.get('account_id', '')}/{target.get('code', '')}：{outcome}")
        lines.append("模型学习：仅记录证据，不自动调整规则或权重；五日内保持规则版本冻结。")
    return "\n".join(lines)


def _format_risk(snapshot, report_kind):
    warned = []
    for account in snapshot.accounts:
        for item in account.holdings:
            for warning in item.warnings:
                warned.append(f"{item.holding.name}：{warning}")
    lines = ["风险与条件"]
    if warned:
        lines.append("；".join(dict.fromkeys(warned)))
    if report_kind == "midday":
        lines.append("下午仍可能修正；晨报参考价变化不能替代开盘以来收益或交易归因。")
    elif report_kind == "trading":
        lines.append("仅记录已触发的风险/技术条件；待数据完整后再复核。")
    elif report_kind == "closing":
        lines.append(
            "收盘后检查事前判断配对、恒生科技联动、基金净值日期与集中度，"
            "不把单日涨跌外推为长期结论。"
        )
    else:
        lines.append("当前报告用于条件复核，不提供即时交易指令。")
    return "\n".join(lines)


def _analysis_map(analysis):
    if not analysis:
        return {}
    return {
        (item.get("account_id"), item.get("code")): item
        for item in analysis.get("items", ())
    }


def _format_analysis_item(item):
    code = item.get("code", "")
    if item.get("status") != "ready":
        return f"技术/缠论：{code} 未分析（{_public_analysis_reason(item)}）。"
    technical = item["technical"]
    trend = technical["trend"]
    kdj = technical["kdj"]
    boll = technical["boll"]
    return (
        f"技术/缠论：技术评分 {technical['score']}（非综合评分），趋势 {trend['direction']}，"
        f"MACD柱 {technical['macd_hist']:+.4f}，RSI {technical['rsi']:.2f}，"
        f"KDJ(J) {kdj['j']:.2f}，布林位置 {_ratio_text(boll['close_position'])}，"
        f"分型/笔/线段/中枢 {item['chan']['fractals']}/"
        f"{item['chan']['strokes']}/{item['chan']['segments']}/"
        f"{item['chan']['zhongshu']}，缠论信号 {item['signal']}，"
        "决策 WAIT（完整证据未就绪，待本人复核）。"
    )


def _format_decision_evidence(item):
    """Render the decision evidence block for one holding.

    Every cycle is read from the upstream structure payload, which is itself
    fail-closed. An absent or unparsable payload degrades every cycle to
    未就绪 rather than assuming a closed bar, and no branch may upgrade a
    cycle into a buy condition: the action line stays a fixed WAIT because
    complete analysis is not reachable yet.
    """
    item = item if isinstance(item, Mapping) else {}
    structure = item.get("structure")
    return "\n".join(
        (
            "多周期证据（缠论结构）：" + _cycle_evidence_text(structure),
            _structure_conclusion_text(structure),
            "完整分析：未就绪；动作 WAIT；建议仓位变化 +0%。补齐来源与风险证据后人工复核。",
        )
    )


_CYCLE_EVIDENCE_ORDER = (
    ("weekly", "周线"),
    ("daily", "日线"),
    ("120m", "120分钟"),
    ("30m", "30分钟"),
    ("15m", "15分钟"),
    ("5m", "5分钟"),
)

_CYCLE_EVIDENCE_TEXT = {
    "closed": "已闭合，结构待确认",
    "forming": "形成中，不参与确认",
    "missing": "缺失",
    "invalid": "无效",
    "stale": "陈旧，不参与确认",
}

_CYCLE_LABEL_BY_VALUE = {value: label for value, label in _CYCLE_EVIDENCE_ORDER}

_STRUCTURE_OUTCOME_TEXT = {
    "CONFIRMED": "结构已确认，仍需本人复核后再决定是否执行",
    "PRECONFIRM": "前置确认中，等闭合周期补齐",
}

_STRUCTURE_REASON_TEXT = {
    "STRUCTURE_DATA_MISSING": "必需周期数据缺失",
    "STRUCTURE_INCOMPLETE": "周期数据不完整",
    "UNCONFIRMED_STRUCTURE": "结构信号未确认",
}


def _cycle_evidence_text(structure):
    """Describe closedness per cycle; unknown values never become 'closed'."""
    statuses = structure.get("per_cycle_status") if isinstance(structure, Mapping) else None
    if not isinstance(statuses, Mapping):
        return "；".join(f"{label} 未就绪" for _, label in _CYCLE_EVIDENCE_ORDER)
    return "；".join(
        f"{label} {_CYCLE_EVIDENCE_TEXT.get(statuses.get(value), '未就绪')}"
        for value, label in _CYCLE_EVIDENCE_ORDER
    )


def _structure_conclusion_text(structure):
    """Summarize the structure gate without promoting the blocked action."""
    if not isinstance(structure, Mapping):
        return "结构结论：未就绪；缺少已验证的多周期结构证据，不生成买入条件。"
    outcome = _STRUCTURE_OUTCOME_TEXT.get(
        structure.get("outcome"), "等待，证据不足不生成买入条件"
    )
    blockers = []
    for value in structure.get("blocked_by") or ():
        label = "核心信号" if value == "core_signal" else _CYCLE_LABEL_BY_VALUE.get(value)
        if label is not None and label not in blockers:
            blockers.append(label)
    detail = "、".join(blockers) or _STRUCTURE_REASON_TEXT.get(structure.get("reason_code"))
    if detail:
        return f"结构结论：{outcome}；待补齐：{detail}。"
    return f"结构结论：{outcome}。"


def _format_minute_context(item):
    context = item.get("minute_context") or {}
    if context.get("configured") is not True:
        return ""
    labels = {"closed": "已闭合", "forming": "形成中", "missing": "缺失",
              "invalid": "无效", "stale": "陈旧"}
    states = context.get("bar_status") or {}
    summary = "，".join(
        f"{cycle} {labels.get(states.get(cycle), '未知')}"
        for cycle in ("120m", "30m", "15m", "5m")
    )
    trigger = context.get("next_trigger") or {}
    when = trigger.get("at") or "条件满足后（不预设时间）"
    return redact_secrets(
        f"分钟复核：{summary}。\n{context.get('reason', '分钟证据不足，WAIT。')}\n"
        f"下一复核点：{when}；{trigger.get('condition', '等待数据齐全。')}"
        f"{trigger.get('action', 'WAIT；仅作人工复核，不自动下单。')}",
        environ={},
    )


def _format_benchmark_analysis(item):
    if item.get("status") != "ready":
        return f"恒生科技技术/缠论：未分析（{_public_analysis_reason(item)}）。"
    technical = item["technical"]
    return (
        f"恒生科技技术/缠论：技术评分 {technical['score']}，趋势 {technical['trend']['direction']}，"
        f"RSI {technical['rsi']:.2f}，信号 {item['signal']}。"
    )


def _format_research_status(analysis, report_kind, global_market=None):
    coverage = (analysis or {}).get("coverage", {})
    limits = (analysis or {}).get("data_limits") or {}
    fundamental_data = (analysis or {}).get("fundamental") or {}
    f_ready = fundamental_data.get("ready")
    if f_ready is None or isinstance(f_ready, bool):
        f_ready = 0
    f_eligible = fundamental_data.get("eligible")
    if f_eligible is None or isinstance(f_eligible, bool):
        f_eligible = 0
    f_criterion_passed = fundamental_data.get("criterion_passed")
    if f_criterion_passed is None or isinstance(f_criterion_passed, bool):
        f_criterion_passed = 0

    fundamental_line = (
        f"基本面：已接入经校验的财报/估值数据源（{f_ready}/{f_eligible} 个 A 股持仓通过校验，{f_criterion_passed} 个满足基本面标准）。"
        if limits.get("fundamental") == "available"
        else "基本面：未接入经校验的财报/估值数据源，不生成基本面结论。"
    )
    industry_ranking_data = (analysis or {}).get("industry_ranking") or {}
    raw_items = industry_ranking_data.get("items") or ()
    valid_items = []
    for it in raw_items:
        score = it.get("score") if isinstance(it, dict) else getattr(it, "score", None)
        if score is None or isinstance(score, bool):
            continue
        try:
            score_float = float(score)
            if not math.isfinite(score_float):
                continue
            rank_val = it.get("rank") if isinstance(it, dict) else getattr(it, "rank", None)
            name_val = it.get("name") if isinstance(it, dict) else getattr(it, "name", None)
            if rank_val is None or name_val is None:
                continue
            valid_items.append((rank_val, name_val, score_float))
        except (TypeError, ValueError):
            continue

    if limits.get("industry") == "available" and valid_items:
        as_of_val = industry_ranking_data.get("as_of")
        if isinstance(as_of_val, str) and len(as_of_val) >= 10:
            as_of_date = as_of_val[:10]
        elif isinstance(as_of_val, (datetime, date)):
            as_of_date = as_of_val.strftime("%Y-%m-%d")
        else:
            as_of_date = str(as_of_val or "")

        coverage_val = industry_ranking_data.get("coverage")
        if coverage_val is None:
            coverage_val = len(valid_items)

        industry_lines = [
            f"行业排序：已接入经校验的行业数据（截至 {as_of_date}，共 {coverage_val} 个板块参与横向比较）。"
        ]
        for rank_val, name_val, score_float in valid_items[:10]:
            industry_lines.append(f"  {rank_val}. {name_val} {score_float:.1f}")
        industry_lines.append(
            "行业排序说明：评分为同一截面内的横向百分位加权，仅用于相对排序，不构成买入信号。"
        )
        lines = [
            "研究完整性",
            "完整分析：未就绪；流程成功不等于研究证据完整。",
            f"技术/缠论：{coverage.get('ready', 0)}/{coverage.get('total', 0)} 个持仓完成，"
            f"{coverage.get('unavailable', 0)} 个因历史数据不足或接口失败降级。",
            fundamental_line,
            "新闻：未接入经校验的新闻源，不把未核实消息写入决策。",
            "市场环境：趋势/风险暂不评级；缺少完整跨市场证据。",
            *industry_lines,
        ]
    else:
        industry_ranking_line = (
            "行业排序：已接入经校验的行业数据。"
            if limits.get("industry") == "available"
            else "行业排序：未就绪；没有可比较的行业数据，不编造行业评分或资金流向。"
        )
        lines = [
            "研究完整性",
            "完整分析：未就绪；流程成功不等于研究证据完整。",
            f"技术/缠论：{coverage.get('ready', 0)}/{coverage.get('total', 0)} 个持仓完成，"
            f"{coverage.get('unavailable', 0)} 个因历史数据不足或接口失败降级。",
            fundamental_line,
            "行业：仅使用持仓配置中的行业标签，用于暴露统计，不等同于行业景气判断。",
            "新闻：未接入经校验的新闻源，不把未核实消息写入决策。",
            "市场环境：趋势/风险暂不评级；缺少完整跨市场证据。",
            industry_ranking_line,
        ]
    if analysis is None:
        lines.append("技术/缠论：本次未执行历史分析。")
    if report_kind in {"global", "morning"}:
        if not isinstance(global_market, dict) or global_market.get("status") == "not_collected":
            lines.append("隔夜全球市场：本阶段不采集。")
        elif global_market.get("status") in {"complete", "partial"}:
            quotes = global_market.get("quotes") or ()
            n = sum(1 for q in quotes if isinstance(q, dict) and q.get("error_code") is None)
            if n == 8:
                lines.append("隔夜全球市场：8 项隔夜行情均已获取，交易时点见各项标注。")
            else:
                lines.append(f"隔夜全球市场：8 项隔夜行情中已获取 {n} 项，其余暂不可用。")
        elif global_market.get("status") == "unavailable":
            lines.append("隔夜全球市场：暂不可用，本次不作方向判断。")
        else:
            lines.append("隔夜全球市场：本阶段不采集。")
    if any((item.get("minute_context") or {}).get("configured") is True
           for item in (analysis or {}).get("items", ())):
        minute_coverage = analysis.get("minute_coverage") or {}
        lines.append(
            f"分钟数据：{minute_coverage.get('ready', 0)}/{minute_coverage.get('total', 0)} "
            "个持仓具备当前时段证据；仅校验闭合，不代表多周期结构确认。"
        )
    else:
        lines.append("分钟数据：未接入经校验的分钟快照；不能据日线推断分钟结构确认。")
    if report_kind == "trading":
        lines.append("决策：只输出已触发的条件化复核，不自动下单。")
    else:
        lines.append("决策：技术与缠论结果仅作为人工复核输入，不自动下单。")
    return "\n".join(lines)


def _ratio_text(value):
    return "未定义" if value is None else f"{Decimal(str(value)) * 100:.1f}%"


def _public_analysis_reason(item):
    """Return a concise user-facing reason without leaking provider internals."""
    reason = str(item.get("reason") or "")
    if reason.startswith("历史K线不足"):
        summary = reason.split("，已排除", 1)[0]
        return summary.replace("历史K线", "历史行情")
    if reason == "历史K线接口未配置":
        return "历史行情接口未配置，已降级为持仓与风险分析"
    return "历史行情暂不可用，已降级为持仓与风险分析"


def _is_unusable(item):
    return item.valuation is None or item.valuation.freshness != "fresh"


def _freshness(valuation):
    if valuation is None:
        return "缺少估值"
    return {"fresh": "新鲜", "stale": "陈旧", "lagged": "滞后", "failed": "失败"}.get(
        valuation.freshness, valuation.freshness
    )


def _money(value):
    return f"{Decimal(str(value)):.2f}"


def _price(value):
    return f"{Decimal(str(value)):.2f}"


def _percent(value):
    return f"{Decimal(str(value)) * 100:+.2f}%"
