"""单笔风险预算——R11：账户资产 × 风险比例（0.5%–1.0%）。"""

from decimal import Decimal

from ._numeric import to_decimal

try:
    from collections.abc import Mapping
except ImportError:  # Python < 3.9 fallback
    from typing import Mapping

DEFAULT_TRADE_RISK_MIN = Decimal("0.005")
DEFAULT_TRADE_RISK_MAX = Decimal("0.010")


def load_risk_rules() -> Mapping:
    """读取 config/rules.yaml；任何读取/解析异常都返回空配置并使用默认区间。

    配置不可用（缺失、权限、编码、YAML 语法错误）属于数据缺失而非
    程序缺陷，必须降级到默认区间（R08），绝不让决策引擎崩溃。
    """
    try:
        from app.config.loader import load_yaml

        return load_yaml("rules.yaml") or {}
    except FileNotFoundError:
        return {}
    except Exception:  # yaml.YAMLError / OSError / UnicodeError 等统一降级
        return {}


def _ratio(value) -> Decimal:
    if isinstance(value, str) and value.strip().endswith("%"):
        return to_decimal(value.strip()[:-1], "风险比例") / Decimal("100")
    return to_decimal(value, "风险比例")


def _risk_section(rules) -> Mapping:
    if rules is None:
        rules = load_risk_rules()
    if not isinstance(rules, Mapping):
        raise ValueError("rules 必须是映射类型")
    section = rules.get("risk")
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ValueError("rules 的 risk 段必须是映射类型")
    return section


def _bounds(section) -> tuple:
    lower = _ratio(section.get("trade_risk_min", DEFAULT_TRADE_RISK_MIN))
    upper = _ratio(section.get("trade_risk_max", DEFAULT_TRADE_RISK_MAX))
    if not 0 < lower <= upper:
        raise ValueError("风险比例区间必须满足 0 < trade_risk_min <= trade_risk_max")
    return lower, upper


def risk_ratio_bounds(rules=None) -> tuple:
    """返回（下限, 上限）风险比例区间，默认 0.5%–1.0%（R11）。"""
    return _bounds(_risk_section(rules))


def calculate_risk_budget(account_assets, risk_ratio=None, rules=None) -> Decimal:
    """单笔风险预算 = 账户资产 × 风险比例；比例越界时显式拒绝（R11）。"""
    assets = to_decimal(account_assets, "账户资产")
    if assets <= 0:
        raise ValueError("账户资产必须为正数")
    section = _risk_section(rules)
    lower, upper = _bounds(section)
    if risk_ratio is None:
        raw_default = section.get("trade_risk_default")
        ratio = _ratio(raw_default) if raw_default is not None else lower
    else:
        ratio = _ratio(risk_ratio)
    if not lower <= ratio <= upper:
        raise ValueError(
            f"单笔风险比例必须在 {lower}–{upper} 区间内（R11：0.5%–1.0%）"
        )
    return assets * ratio
