from __future__ import annotations

from typing import Mapping, Any


SELL_PUT_NOTIFICATION_HIGH = "通过准入后，收益/风险组合较强，值得优先看。"
SELL_PUT_NOTIFICATION_MEDIUM = "已通过准入，可作为今日观察候选。"
SELL_PUT_NOTIFICATION_LOW = "已通过准入，但优先级一般。"

SELL_CALL_NOTIFICATION_HIGH = "通过准入后，权利金回报与行权空间比较平衡。"
SELL_CALL_NOTIFICATION_MEDIUM = "已通过准入，可作为 sell call 备选。"
SELL_CALL_NOTIFICATION_LOW = "已通过准入，但优先级一般。"


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def render_sell_put_comment(row: Mapping[str, Any]) -> str:
    risk = str(row.get("risk_label", "未知") or "未知")
    annual = _to_float(row.get("annualized_net_return_on_cash_basis", 0), 0.0)
    spread = _to_float(row.get("spread_ratio", 1), 1.0)

    if risk == "激进":
        return "年化很高，但离现价较近，偏激进。"
    if annual >= 0.20 and spread <= 0.20:
        return "收益和安全边际比较平衡，可优先看。"
    if annual >= 0.12:
        return "收益尚可，整体可考虑。"
    return "可作为备选观察。"


def render_sell_call_comment(row: Mapping[str, Any]) -> str:
    risk = str(row.get("risk_label", "未知") or "未知")
    annual = _to_float(row.get("annualized_net_premium_return", 0), 0.0)
    total = _to_float(row.get("if_exercised_total_return", 0), 0.0)

    if risk == "激进":
        return "权利金不错，但行权价离现价较近，更容易卖飞。"
    if annual >= 0.10 and total >= 0.15:
        return "权利金收益和被行权后的总收益都比较平衡，可优先看。"
    if annual >= 0.06:
        return "收益尚可，适合作为 sell call 备选。"
    return "可作为备选观察。"
