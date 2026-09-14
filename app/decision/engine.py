from dataclasses import dataclass
from decimal import Decimal

from app.chan.signal import ChanSignal
from app.decision.gates import evaluate_gates
from app.risk._numeric import to_decimal


@dataclass(frozen=True)
class Decision:
    action: str
    signal: ChanSignal
    position_percent: float
    reasons: tuple
    auto_execute: bool = False
    blocked_by: tuple = ()
    new_evidence: tuple = ()


BUY_SIGNALS = (
    ChanSignal.FIRST_BUY,
    ChanSignal.CLASS_SECOND_BUY,
    ChanSignal.SECOND_BUY,
    ChanSignal.THIRD_BUY,
)


def build_decision(
    signal: ChanSignal,
    technical_score: float,
    max_position: float = 0.30,
    risk_blocked: bool = False,
    current_position: float = 0.0,
    risk_context=None,
    new_evidence: tuple = (),
    gates=None,
    rules=None,
):
    """Signal ≠ Decision（R01）：结构信号只是输入，动作由本引擎输出。

    - 有风险上下文时仓位由风险预算推导（R11）并经 RR 门（R12）与
      各上限裁剪；无上下文时仓位为 0 并注明研究参考（R08）。
    - auto_execute 恒为 False（R18）。
    - ADD 必须携带非空新证据，否则降级 HOLD。
    """
    if not isinstance(signal, ChanSignal):
        raise ValueError("signal must be a ChanSignal")
    if not isinstance(risk_blocked, bool):
        raise ValueError("risk_blocked must be a bool")
    current_position = _unit_interval(current_position, "current_position")
    evidence = _normalize_evidence(new_evidence)

    if risk_blocked or signal == ChanSignal.SELL_RISK:
        return Decision(
            "REDUCE" if current_position > 0 else "WAIT",
            signal,
            0.0,
            ("风险条件触发，需人工复核",),
            False,
            ("GATE_6_RISK",),
            evidence,
        )

    max_position = _unit_interval(max_position, "max_position")
    if signal not in BUY_SIGNALS:
        return Decision(
            _blocked_action(current_position),
            signal,
            0.0,
            ("未满足新增仓位条件",),
            False,
            ("STRUCTURE_SIGNAL_UNCONFIRMED",),
            evidence,
        )

    score, score_error = _technical_score(technical_score)
    if score_error is not None:
        return Decision(
            _blocked_action(current_position),
            signal,
            0.0,
            (score_error,),
            False,
            (
                "TECHNICAL_SCORE_MISSING"
                if technical_score is None
                else "TECHNICAL_SCORE_INVALID",
            ),
            evidence,
        )

    gate_result = evaluate_gates(gates)
    if not gate_result.passed:
        return Decision(
            _blocked_action(current_position),
            signal,
            0.0,
            ("七道门未全部通过，拒绝新增仓位",),
            False,
            gate_result.blocked_by,
            evidence,
        )

    if score < Decimal("60"):
        return Decision(
            _blocked_action(current_position),
            signal,
            0.0,
            ("技术指标辅助评分不足，等待结构质量改善",),
            False,
            ("TECHNICAL_SCORE_UNCONFIRMED",),
            evidence,
        )

    return _buy_decision(
        signal,
        max_position,
        current_position,
        risk_context,
        evidence,
        rules,
    )


def _blocked_action(current_position):
    """未开仓时被阻断为 WAIT，已持仓时为 HOLD（与既有约定一致）。"""
    return "HOLD" if current_position > 0 else "WAIT"


def _unit_interval(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be between 0 and 1")
    try:
        result = to_decimal(value, name)
    except ValueError:
        raise ValueError(f"{name} must be between 0 and 1") from None
    if not Decimal("0") <= result <= Decimal("1"):
        raise ValueError(f"{name} must be between 0 and 1")
    return result


def _technical_score(value):
    if value is None:
        return None, "技术评分缺失，按数据不足降级"
    if isinstance(value, bool):
        return None, "技术评分无效，按数据异常降级"
    try:
        return to_decimal(value, "technical_score"), None
    except ValueError:
        return None, "技术评分无效，按数据异常降级"


def _normalize_evidence(value):
    if value is None:
        return ()
    if isinstance(value, str):
        values = (value,)
    else:
        try:
            values = tuple(value)
        except TypeError:
            values = (value,)
    return tuple(
        item
        for item in values
        if item is not None and not (isinstance(item, str) and not item.strip())
    )


def _buy_decision(
    signal, max_position, current_position, risk_context, evidence, rules
):
    if not evidence and current_position > 0:
        return Decision(
            "HOLD",
            signal,
            0.0,
            ("已有持仓且未提供新增证据，不满足 ADD 条件，降级为 HOLD",),
            False,
            ("EVIDENCE_REQUIRED_FOR_ADD",),
            (),
        )

    if risk_context is None:
        return Decision(
            _blocked_action(current_position),
            signal,
            0.0,
            ("未提供风险上下文，仓位未计算（研究参考）",),
            False,
            ("GATE_6_RISK_INPUT",),
            evidence,
        )

    from app.risk import (
        RiskContext,
        calculate_position_size,
        calculate_risk_budget,
        calculate_rr,
        rr_thresholds,
    )

    try:
        minimum_rr, preferred_rr = rr_thresholds(rules)
    except (ValueError, TypeError) as exc:
        return Decision(
            _blocked_action(current_position),
            signal,
            0.0,
            (f"风险规则配置无效，拒绝开仓：{exc}",),
            False,
            ("GATE_6_RISK_CONFIG",),
            evidence,
        )

    try:
        if not isinstance(risk_context, RiskContext):
            risk_context = RiskContext(
                account_assets=risk_context["account_assets"],
                entry=risk_context["entry"],
                target=risk_context["target"],
                invalidation=risk_context["invalidation"],
                risk_ratio=risk_context.get("risk_ratio"),
            )

        rr = calculate_rr(
            risk_context.entry, risk_context.target, risk_context.invalidation
        )
        risk_amount = calculate_risk_budget(
            risk_context.account_assets,
            risk_ratio=risk_context.risk_ratio,
            rules=rules,
        )
        position = calculate_position_size(
            risk_amount,
            risk_context.entry,
            risk_context.invalidation,
            risk_context.account_assets,
            (
                (
                    "max_position",
                    max(Decimal("0"), max_position - current_position),
                ),
                (
                    "remaining_room",
                    max(Decimal("0"), Decimal("1") - current_position),
                ),
            ),
        )
    except (ValueError, KeyError, TypeError) as exc:
        return Decision(
            _blocked_action(current_position),
            signal,
            0.0,
            (f"风险输入无效，拒绝开仓：{exc}",),
            False,
            ("GATE_6_RISK_INPUT",),
            evidence,
        )

    if rr < minimum_rr:
        return Decision(
            _blocked_action(current_position),
            signal,
            0.0,
            (f"盈亏比 {rr:.2f} 低于下限 {minimum_rr:.2f}，拒绝新开仓",),
            False,
            ("GATE_6_RISK_RR",),
            evidence,
        )

    if position.position_percent <= 0:
        return Decision(
            _blocked_action(current_position),
            signal,
            0.0,
            ("可用仓位为 0（已达上限或满仓），拒绝开仓",),
            False,
            ("GATE_6_RISK_POSITION",),
            evidence,
        )

    reasons = [
        "结构信号与技术评分同时通过",
        f"盈亏比 {rr:.2f} 通过最低门槛",
    ]
    if rr >= preferred_rr:
        reasons.append(f"盈亏比达到优选区间 {preferred_rr:.2f}+")
    else:
        reasons.append(f"盈亏比尚未达到优选值 {preferred_rr:.2f}，仅通过硬门槛")
    if position.capped_by:
        reasons.append(f"仓位受上限裁剪：{', '.join(position.capped_by)}")
    if current_position > 0:
        reasons.append("新增证据：" + "；".join(map(str, evidence)))
    reasons.append("仅生成研究建议，不自动交易")
    return Decision(
        "ADD" if current_position > 0 else "BUY",
        signal,
        float(position.position_percent),
        tuple(reasons),
        False,
        (),
        evidence,
    )
