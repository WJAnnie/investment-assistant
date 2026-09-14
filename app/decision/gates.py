"""Fail-closed seven-gate contract for BUY/ADD decisions (R07)."""

from collections.abc import Mapping
from dataclasses import dataclass


GATE_CODES = {
    "market": "GATE_1_MARKET",
    "asset": "GATE_2_ASSET",
    "fundamental": "GATE_3_FUNDAMENTAL",
    "valuation": "GATE_4_VALUATION",
    "structure": "GATE_5_STRUCTURE",
    "risk": "GATE_6_RISK",
    "execution": "GATE_7_EXECUTION",
}


@dataclass(frozen=True)
class GateEvaluation:
    passed: bool
    blocked_by: tuple[str, ...]


def _explicitly_passed(value) -> bool:
    if value is True:
        return True
    if isinstance(value, Mapping):
        return value.get("passed") is True
    return getattr(value, "passed", None) is True


def evaluate_gates(gates) -> GateEvaluation:
    """Evaluate every gate independently; missing/unknown values never pass."""
    values = gates if isinstance(gates, Mapping) else {}
    blocked = []
    for name, code in GATE_CODES.items():
        try:
            passed = _explicitly_passed(values.get(name))
        except Exception:
            passed = False
        if not passed:
            blocked.append(code)
    blocked = tuple(blocked)
    return GateEvaluation(not blocked, blocked)
