"""Deterministic rendering of structured API results.

Key numbers and states shown to group members come only from this module,
never from model prose.  It formats existing fields; it never recomputes a
price, fee, yield or recommendation.
"""
from __future__ import annotations

from typing import Any

QUOTE_STATUS_LABELS = {
    "current": "当前",
    "last_known": "最近已知",
    "delayed": "延迟",
    "unavailable": "不可用",
}

OUTCOME_LABELS = {
    "found": "找到候选",
    "none": "无可展示候选",
    "unable": "无法完成搜索",
}


def _number(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "不可用"
    return f"{float(value):.{digits}f}"


def _text(value: Any) -> str:
    return str(value) if value not in (None, "") else "不可用"


def _primary_quote(quote: dict[str, Any] | None) -> str:
    if not isinstance(quote, dict):
        return "报价不可用"
    bits: list[str] = []
    for label, key in (("最新", "last_price"), ("买", "bid"), ("卖", "ask")):
        if quote.get(key) is not None:
            bits.append(f"{label} {_number(quote[key], 4)}")
    if quote.get("delta") is not None:
        bits.append(f"Delta {_number(quote['delta'], 3)}")
    observed = quote.get("observed_at_utc") or quote.get("snapshot_received_at_utc")
    if observed:
        bits.append(f"观察时间 {observed}")
    if quote.get("market_state"):
        bits.append(f"市场状态 {quote['market_state']}")
    return "；".join(bits) if bits else "报价不可用"


def render_quotes(data: dict[str, Any]) -> str:
    status = QUOTE_STATUS_LABELS.get(str(data.get("quote_status")), str(data.get("quote_status")))
    requested = (data.get("effective_parameters") or {}).get("contract_requested")
    lines = [f"行情状态：{status}"]
    lines.append(f"标的：{_primary_quote(data.get('underlier'))}")
    if requested:
        lines.append(f"合约：{_primary_quote(data.get('option'))}")
    missing = data.get("missing_requirements") or []
    if missing:
        lines.append("缺失：" + "、".join(str(item) for item in missing))
    return "\n".join(lines)


def _candidate_line(index: int, candidate: dict[str, Any]) -> str:
    metrics = candidate.get("metrics")
    if not isinstance(metrics, dict):
        metrics = candidate
    contract = candidate.get("option_contract")
    if not isinstance(contract, dict):
        contract = {}
    expiration = candidate.get("expiration") or contract.get("expiration")
    strike = candidate.get("strike") if candidate.get("strike") is not None \
        else contract.get("strike")
    bits = [
        f"{index}. {_text(candidate.get('contract_symbol'))}",
        f"到期 {_text(expiration)}",
        f"行权价 {_number(strike, 2)}",
        f"买 {_number(candidate.get('bid'), 4)}/卖 {_number(candidate.get('ask'), 4)}",
    ]
    if candidate.get("delta") is not None:
        bits.append(f"Delta {_number(candidate['delta'], 3)}")
    if candidate.get("dte") is not None:
        bits.append(f"DTE {candidate['dte']}")
    annualized = metrics.get("incremental_annualized_return")
    if annualized is None:
        annualized = metrics.get("annualized_net_return")
    if annualized is None:
        annualized = metrics.get("annualized_return")
    if annualized is not None:
        bits.append(f"年化 {_number(annualized * 100, 2)}%")
    if candidate.get("economic_result") is not None:
        bits.append(f"经济结果 {candidate['economic_result']}")
    return "；".join(bits)


def render_candidates(data: dict[str, Any]) -> str:
    outcome = OUTCOME_LABELS.get(str(data.get("outcome")), str(data.get("status")))
    lines = [f"候选状态：{outcome}"]
    candidates = data.get("candidates") or []
    for index, candidate in enumerate(candidates, start=1):
        lines.append(_candidate_line(index, candidate))
    if not candidates:
        lines.append("没有可展示的候选。")
    lines.append(f"展示 {int(data.get('displayed') or 0)} 条，排除 "
                 f"{int(data.get('omitted') or 0)} 条。")
    for code in data.get("reason_codes") or []:
        lines.append(f"原因：{code}")
    return "\n".join(lines)


def _metrics_lines(metrics: dict[str, Any] | None) -> list[str]:
    if not isinstance(metrics, dict):
        return []
    lines = []
    for key, value in metrics.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            lines.append(f"{key}：{_number(value, 4)}")
        elif value not in (None, ""):
            lines.append(f"{key}：{value}")
    return lines


def render_close(data: dict[str, Any]) -> str:
    lines = [
        f"平仓评估：{_text(data.get('recommendation_state'))} / {_text(data.get('evaluation_status'))}",
        f"原合约报价：{_primary_quote(data.get('quote'))}",
    ]
    lines.extend(_metrics_lines(data.get("metrics")))
    fee = data.get("fee_model") or {}
    if isinstance(fee, dict):
        lines.append(
            f"费用模型 {_text(fee.get('id') or fee.get('model_id'))}"
            f"@{_text(fee.get('version'))}："
            f"开仓费 {_number(fee.get('estimated_open_fee'), 2)}，"
            f"平仓费 {_number(fee.get('estimated_close_fee'), 2)}"
            f"（{_text(fee.get('fee_calc_status'))}）")
    lines.append("按富途标准费用模型估算，非账户实际费用。")
    if data.get("reason"):
        lines.append(f"理由：{data['reason']}")
    for code in data.get("reason_codes") or []:
        lines.append(f"原因：{code}")
    lines.append(f"评估时间：{_text(data.get('evaluated_at_utc'))}")
    return "\n".join(lines)


def render_roll(data: dict[str, Any]) -> str:
    outcome = OUTCOME_LABELS.get(str(data.get("outcome")), str(data.get("outcome")))
    lines = [
        f"移仓搜索：{outcome}（{_text(data.get('status'))}）",
        f"原合约：{_primary_quote(data.get('old_quote'))}",
    ]
    if data.get("analysis_kind") == "conditional":
        coverage = data.get("stock_coverage")
        if (not isinstance(coverage, dict)
                or coverage.get("verification_status") != "not_verified"):
            return "结果合同不一致：缺少未经核验的股票覆盖字段，拒绝展示。"
        if not data.get("unverified_conditions"):
            return "结果合同不一致：缺少未核验条件，拒绝展示。"
        lines.append("条件分析：股票覆盖未经核实，不构成正式账户建议。")
    candidates = data.get("candidates") or []
    for index, candidate in enumerate(candidates, start=1):
        lines.append(_candidate_line(index, candidate))
    if not candidates:
        lines.append("没有可展示的候选。")
    lines.append(f"展示 {int(data.get('displayed') or 0)} 条，排除 "
                 f"{int(data.get('omitted') or 0)} 条。")
    scope = data.get("scope") or {}
    if isinstance(scope, dict):
        for key, value in scope.items():
            lines.append(f"范围 {key}：{value}")
    for code in data.get("reason_codes") or []:
        lines.append(f"原因：{code}")
    lines.append("按富途标准费用模型估算，非账户实际费用。")
    lines.append(f"评估时间：{_text(data.get('evaluated_at_utc'))}")
    return "\n".join(lines)


def render_result(method: str, data: dict[str, Any]) -> str:
    if method == "query_quotes":
        return render_quotes(data)
    if method == "query_candidates":
        return render_candidates(data)
    if method == "evaluate_close":
        return render_close(data)
    if method in {"evaluate_roll_down", "evaluate_roll_up"}:
        return render_roll(data)
    raise ValueError("unsupported group-query method")


def compose_reply(explanation: str | None, rendered: str | None) -> str:
    """Model prose may explain; the rendered block is the numeric authority."""
    prose = (explanation or "").strip()
    if rendered is None:
        return prose
    if not prose:
        return rendered
    return f"{prose}\n\n{rendered}"


def render_admission_rejected(retry_after_seconds: int) -> str:
    return f"请求过于频繁，请约 {max(1, int(retry_after_seconds))} 秒后重试。"


def render_budget_exhausted(reason: str) -> str:
    return f"本次查询在限制内未能完成（{reason}），请缩小范围或稍后重试。"


__all__ = [
    "compose_reply",
    "render_admission_rejected",
    "render_budget_exhausted",
    "render_candidates",
    "render_close",
    "render_quotes",
    "render_result",
    "render_roll",
]
