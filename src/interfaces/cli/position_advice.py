"""Human projection of the existing assessment contract; no policy or I/O."""
from __future__ import annotations

from typing import Any


def _value(value: Any, *, percent: bool = False) -> str:
    if value is None or value == "":
        return "不可用"
    if percent and isinstance(value, (int, float)):
        return f"{value:.2%}"
    return str(value)


def render_position_advice(result: dict[str, Any]) -> str:
    if result.get("ok") is False:
        error = result["error"]
        lines = [f"持仓评估执行失败 [{error['code']}]：{error['message']}"]
        if error.get("hint"):
            lines.append(error["hint"])
        return "\n".join(lines) + "\n"
    status = result.get("status")
    label = {"success": "成功", "partial": "部分结果（含不可评估持仓）",
             "disabled": "持仓建议未启用", "unavailable": "持仓建议插件不可用"}.get(status, _value(status))
    rows = result.get("rows", [])
    lines = [f"持仓评估：{label}"]
    if result.get("reason"):
        lines.append(str(result["reason"]))
    if result.get("account"):
        lines.append(f"账户：{result['account']}；请求时间：{_value(result.get('evaluated_at_utc'))}")
    if status == "success" and not rows:
        lines.append("无匹配持仓（合法空结果）")
    for row in rows:
        mode = row.get("assessment_mode")
        mode_label = {"current": "当前评估", "historical_reference": "历史参考（不代表当前建议）",
                      "unavailable": "不可评估"}.get(mode, "不可评估")
        lines.extend([
            "",
            f"合约：{_value(row.get('symbol'))} {_value(row.get('option_type'))} "
            f"{_value(row.get('expiration') or row.get('expiration_ymd'))} "
            f"行权价 {_value(row.get('strike'))}；张数 {_value(row.get('contracts_open'))}",
            f"评估模式：{mode_label}；数据时间：{_value(row.get('assessment_as_of_utc'))}",
            f"现价：{_value(row.get('spot'))}；买/卖价：{_value(row.get('bid'))}/{_value(row.get('ask'))}；DTE：{_value(row.get('dte'))}",
            f"净兑现比例：{_value(row.get('net_capture_ratio'), percent=True)}；"
            f"剩余最高年化：{_value(row.get('remaining_max_annualized_return'), percent=True)}；"
            f"资金分母：{_value(row.get('capital_basis'))} {_value(row.get('currency'))}",
        ])
        decision = row.get("recommendation_state")
        labels = {"hold": "本策略未触发平仓条件（hold）", "close": "可考虑买回平仓（close）"}
        if mode == "historical_reference":
            reference = row.get("reference_recommendation_state")
            lines.append(f"历史结论：{labels.get(reference, '不可评估')}；当前不可据此平仓")
        elif mode == "current" and row.get("current_actionable") is True:
            lines.append(f"结论：{labels.get(decision, '不可评估')}")
        else:
            lines.append("结论：不可评估")
        lines.append(f"原因：{_value(row.get('reason'))}")
    if result.get("report_path"):
        lines.append(f"\n独立报告：{result['report_path']}")
    lines.append("hold 仅表示现有策略未触发平仓条件；休市历史参考不代表当前平仓建议。")
    return "\n".join(lines) + "\n"
