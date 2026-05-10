from __future__ import annotations

from typing import Mapping, Any

from domain.domain.alert_policy import (
    DEFAULT_ALERT_POLICY,
    AlertPolicy,
    resolve_alert_policy,
)


SELL_PUT_NOTIFICATION_HIGH = "通过准入后，收益/风险组合较强，值得优先看。"
SELL_PUT_NOTIFICATION_MEDIUM = "已通过准入，可作为今日观察候选。"
SELL_PUT_NOTIFICATION_LOW = "已通过准入，但优先级一般。"

SELL_CALL_NOTIFICATION_HIGH = "通过准入后，权利金回报与行权空间比较平衡。"
SELL_CALL_NOTIFICATION_MEDIUM = "已通过准入，可作为 sell call 备选。"
SELL_CALL_NOTIFICATION_LOW = "已通过准入，但优先级一般。"


# Module-level active policy lets the pipeline runtime inject user config so
# that the render_*_comment helpers, invoked as same-process callables, pick up
# account-specific thresholds without altering their signatures or callers.
_active_policy: AlertPolicy = DEFAULT_ALERT_POLICY


def set_active_alert_policy(policy: AlertPolicy | Mapping[str, Any] | None) -> AlertPolicy:
    # Public injection point. Accepts a resolved AlertPolicy, a raw mapping
    # (validated config dict), or None to reset to DEFAULT_ALERT_POLICY.
    global _active_policy
    if policy is None:
        _active_policy = DEFAULT_ALERT_POLICY
    elif isinstance(policy, AlertPolicy):
        _active_policy = policy
    elif isinstance(policy, Mapping):
        _active_policy = resolve_alert_policy(dict(policy))
    else:
        _active_policy = DEFAULT_ALERT_POLICY
    return _active_policy


def get_active_alert_policy() -> AlertPolicy:
    return _active_policy


def _coerce_policy(policy: AlertPolicy | Mapping[str, Any] | None) -> AlertPolicy:
    if policy is None:
        return _active_policy
    if isinstance(policy, AlertPolicy):
        return policy
    if isinstance(policy, Mapping):
        return resolve_alert_policy(dict(policy))
    return _active_policy


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def render_sell_put_comment(
    row: Mapping[str, Any],
    *,
    policy: AlertPolicy | Mapping[str, Any] | None = None,
) -> str:
    risk = str(row.get("risk_label", "未知") or "未知")
    annual = _to_float(row.get("annualized_net_return_on_cash_basis", 0), 0.0)
    spread = _to_float(row.get("spread_ratio", 1), 1.0)
    cfg = _coerce_policy(policy).sell_put

    if risk == "激进":
        return "年化很高，但离现价较近，偏激进。"
    if annual >= cfg.high_annual and spread <= cfg.high_spread_max:
        return "收益和安全边际比较平衡，可优先看。"
    if annual >= cfg.medium_annual:
        return "收益尚可，整体可考虑。"
    return "可作为备选观察。"


def render_sell_call_comment(
    row: Mapping[str, Any],
    *,
    policy: AlertPolicy | Mapping[str, Any] | None = None,
) -> str:
    risk = str(row.get("risk_label", "未知") or "未知")
    annual = _to_float(row.get("annualized_net_premium_return", 0), 0.0)
    total = _to_float(row.get("if_exercised_total_return", 0), 0.0)
    cfg = _coerce_policy(policy).sell_call

    if risk == "激进":
        return "权利金不错，但行权价离现价较近，更容易卖飞。"
    if annual >= cfg.high_annual and total >= cfg.high_total:
        return "权利金收益和被行权后的总收益都比较平衡，可优先看。"
    if annual >= cfg.medium_annual:
        return "收益尚可，适合作为 sell call 备选。"
    return "可作为备选观察。"
