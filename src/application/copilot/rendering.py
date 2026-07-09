from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.application.copilot.contracts import AnswerReport, AppResult


VIEW_LABELS = {
    "account_monthly_performance": "月度账户表现",
    "account_monthly_income_components": "月度收益构成",
    "monthly_income_summary": "月度收益汇总",
    "symbol_income_attribution": "标的收益归因",
    "trade_events": "交易事件",
    "open_option_exposure": "当前未平仓期权敞口",
    "expiration_risk_buckets": "到期分布风险",
    "close_advice_snapshot": "当前平仓建议快照",
}

SNAPSHOT_VIEW_NAMES = {"open_option_exposure", "expiration_risk_buckets", "close_advice_snapshot"}
INTERNAL_ATTEMPTED_CHECKS = {"eval_model"}

TOOL_LABELS = {
    "analysis_catalog": "分析视图目录",
    "analysis_query": "分析视图",
    "candidate_filter_explain": "候选过滤诊断",
    "healthcheck": "健康检查",
    "monthly_income_report": "月度收益报告",
    "option_positions_read": "期权持仓",
    "close_advice_read": "平仓建议",
    "runtime_status": "运行状态",
    "scheduler_status": "调度状态",
}

MONTHLY_INCOME_FIELD_LABELS = {
    "closed_lots": "平仓记录",
    "income_rows": "收益明细",
    "premium": "权利金",
    "trade_events": "交易事件",
}

MISSING_DATA_LABELS = {
    "allowed tool observation": "缺少允许工具的观察结果",
    "cited findings": "缺少带引用的发现",
    "cited recommendations": "缺少带引用的建议",
    "concrete recommendations": "建议过于泛化，缺少具体动作或范围",
    "conclusion finding support": "结论缺少对应的发现支持",
    "conclusion missing-data support": "结论点名的缺失对象没有对应缺口记录",
    "conclusion target support": "结论点名的标的缺少对应发现支持",
    "conclusion account support": "结论点名的账户缺少对应发现支持",
    "conclusion numeric support": "结论里的数值缺少对应发现支持",
    "contract_rejected": "执行合同被拒绝",
    "engine_failed": "执行引擎失败",
    "evidence-use boundary": "引用证据的用途超出该证据边界",
    "eval fixture answer report": "评估夹具缺少答案报告",
    "eval fixture disclosure": "评估夹具不是生产证据",
    "eval model answer report": "评估模型未生成答案报告",
    "finding evidence": "发现缺少完整证据引用",
    "fixture observations are not production evidence": "评估夹具不是生产证据",
    "analysis_catalog views": "分析视图目录为空",
    "ambiguous_model_source": "模型来源不唯一",
    "assistant_model_api_key_missing": "assistant 模型 API key 环境变量未配置",
    "assistant_config_not_found": "assistant 配置文件不存在",
    "invalid_assistant_config": "assistant 配置无效",
    "invalid_model_action": "显式模型 action 无效",
    "invalid_model_config": "模型配置无效",
    "finding dimension support": "发现缺少与分析维度一致的内容",
    "malformed recommendations": "建议格式不完整",
    "empty_result": "结果为空",
    "candidate filter trace rows": "候选过滤诊断证据不足",
    "channel_prepare_contract_failed": "渠道执行合同准备失败",
    "channel_model_config_missing": "渠道入口缺少显式 assistant 模型配置",
    "channel_model_api_key_missing": "assistant 模型 API key 环境变量未配置",
    "channel_model_profile_missing": "assistant 配置中没有可用模型",
    "channel_scene_allowlist_missing": "渠道入口没有显式开放任何 Copilot 场景",
    "channel_scene_not_enabled": "渠道入口没有显式开放该 Copilot 场景",
    "channel_run_failed": "渠道 Copilot 执行失败",
    "missing tool evidence": "缺少工具证据",
    "missing_attempted_checks": "缺少已尝试检查",
    "missing_conclusion_prefix": "结论格式无效",
    "model_synthesis_invalid_action": "模型答案动作无效",
    "model_synthesis_not_enabled": "模型答案综合尚未启用",
    "model_synthesis_unavailable": "模型答案综合不可用",
    "model_action_conflicts_with_model_config": "显式模型 action 与模型配置冲突",
    "model_action_requires_eval": "显式模型 action 只能用于 eval",
    "model_api_key_missing": "模型 API key 环境变量未配置",
    "malformed_findings": "发现结构无效",
    "malformed_recommendations": "建议结构无效",
    "malformed_report_fields": "报告字段结构无效",
    "mutation_claim": "结果声称执行了写入或外部操作",
    "no_observations": "没有拿到只读观察结果",
    "non-claimable evidence refs": "引用了不可作为结论依据的证据",
    "omitted evidence boundary": "证据摘要已截断，不能支持穷尽性结论",
    "option_positions_read rows": "期权持仓证据不足",
    "recommendation evidence": "建议缺少完整证据引用",
    "recommendation dimension evidence": "建议引用的证据不支持该分析维度",
    "recommendation dimension support": "建议缺少与分析维度一致的支撑发现",
    "recommendation finding support": "建议没有对应的发现支持",
    "recommendation answer dimension": "建议缺少有效分析维度",
    "recommendation target support": "建议点名的标的缺少对应发现支持",
    "recommendation account support": "建议点名的账户缺少对应发现支持",
    "recommendation numeric support": "建议里的数值缺少对应发现支持",
    "recommendations blocked by missing evidence": "证据缺失时不能给出建议",
    "remaining agent/tool work": "还有未完成的 Agent 或工具步骤",
    "remaining tool observations": "还有未完成的工具观察",
    "requested-period finding evidence": "发现缺少请求期间证据引用",
    "requested-period recommendation evidence": "建议缺少请求期间证据引用",
    "answer-dimension findings": "发现缺少可用分析维度覆盖",
    "invalid_status": "结果状态无效",
    "result_admission_failed": "结果未通过结构或安全校验",
    "scene_manifest": "执行场景准备失败",
    "scene_not_channel_ready": "该场景尚未开放到渠道入口",
    "substantive conclusion": "结论缺少实际判断",
    "substantive findings": "建议缺少有分析含义的发现支持",
    "synthesized answer report": "答案仍像原始明细，未形成综合结论",
    "tool evidence": "工具证据不足",
    "valid agent action": "缺少有效的 Agent 动作",
    "valid conclusion": "缺少以结论开头的有效回答",
    "visible evidence refs": "缺少可见证据引用",
}

ANSWER_DIMENSION_LABELS = {
    "profit quality": "收益质量",
    "assignment cash outlay": "行权现金占用",
    "open-exposure concentration": "持仓暴露集中",
    "current close-advice signals": "当前平仓建议",
    "evidence gaps": "证据缺口",
}

DIAGNOSTIC_STATUS_LABELS = {
    "artifact_missing": "诊断文件缺失",
    "diagnostic_missing": "诊断证据源缺失",
    "empty_artifact": "诊断文件为空",
    "no_matching_rows": "没有匹配诊断行",
    "read_error": "诊断文件读取失败",
}

DIAGNOSTIC_SEVERITY_LABELS = {
    "warning": "警告",
}

DIAGNOSTIC_COUNT_LABELS = {
    "warning_count": "警告数",
    "missing_view_count": "缺失视图数",
    "stale_view_count": "过期视图数",
}


def render_user_response(result: AppResult) -> AppResult:
    if result.user_response.strip() or not result.answer_report:
        return result
    return replace(result, user_response=_render_report(result.answer_report, _ref_tool_labels(result.events)))


def _render_report(report: AnswerReport, ref_labels: dict[str, str] | None = None) -> str:
    lines = [report.conclusion]
    if report.attempted_checks or report.findings or report.missing_data:
        checks = [
            _tool_text(item)
            for item in report.attempted_checks
            if str(item or "").strip() not in INTERNAL_ATTEMPTED_CHECKS
        ]
        lines.append(f"已尝试检查：{', '.join(item for item in checks if item) or '无'}。")
    for finding in report.findings:
        summary = str(finding.get("summary") or "").strip()
        if not summary:
            continue
        ref_text = _ref_text(finding.get("evidence_refs"), ref_labels)
        lines.append(f"- {ref_text}{summary}")
    for recommendation in report.recommendations:
        summary = str(recommendation.get("summary") or "").strip()
        if not summary:
            continue
        ref_text = _ref_text(recommendation.get("basis_refs"), ref_labels)
        lines.append(f"- 建议：{_recommendation_heading(recommendation)}{ref_text}{summary}")
    if report.missing_data:
        missing = [_missing_data_text(item) for item in report.missing_data]
        lines.append("缺口：" + "；".join(item for item in missing if item) + "。")
    return "\n".join(lines)


def _missing_data_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if text in MISSING_DATA_LABELS:
        return MISSING_DATA_LABELS[text]
    if text.startswith("analysis_query filtered view empty: "):
        view_name = text.removeprefix("analysis_query filtered view empty: ").strip()
        label = VIEW_LABELS.get(view_name, view_name)
        return f"{label}为空" if view_name in SNAPSHOT_VIEW_NAMES else f"请求范围内的{label}为空"
    if text.startswith("analysis_query filtered view rows unavailable: "):
        view_name = text.removeprefix("analysis_query filtered view rows unavailable: ").strip()
        label = VIEW_LABELS.get(view_name, view_name)
        return f"请求范围内的{label}缺少可分析明细"
    if text.startswith("analysis_query diagnostic: "):
        return _analysis_diagnostic_text(text.removeprefix("analysis_query diagnostic: ").strip())
    if text.startswith("monthly_income_report missing fields: "):
        fields = [
            MONTHLY_INCOME_FIELD_LABELS.get(item.strip(), item.strip())
            for item in text.removeprefix("monthly_income_report missing fields: ").split(",")
            if item.strip()
        ]
        return "月度收益报告缺少：" + "、".join(fields) if fields else text
    if text == "monthly_income_report evidence":
        return "月度收益报告证据不足"
    if text == "monthly_income_report no matched trade_events":
        return "请求月份没有匹配的本地交易事件"
    if text == "monthly_income_report detail rows":
        return "月度收益报告缺少收益构成明细"
    if text.startswith("monthly_income_report diagnostic status: "):
        status = text.removeprefix("monthly_income_report diagnostic status: ").strip()
        return "月度收益报告诊断状态为空" if status == "empty" else f"月度收益报告诊断状态：{status}"
    if text.startswith("required evidence missing: "):
        tools = [
            TOOL_LABELS.get(item.strip(), item.strip())
            for item in text.removeprefix("required evidence missing: ").split(",")
            if item.strip()
        ]
        return "缺少必需证据：" + "、".join(tools) if tools else text
    if text.startswith("required finding citation missing: "):
        tools = [
            TOOL_LABELS.get(item.strip(), item.strip())
            for item in text.removeprefix("required finding citation missing: ").split(",")
            if item.strip()
        ]
        return "发现缺少完整证据引用：" + "、".join(tools) if tools else text
    if text.startswith("required recommendation citation missing: "):
        tools = [
            TOOL_LABELS.get(item.strip(), item.strip())
            for item in text.removeprefix("required recommendation citation missing: ").split(",")
            if item.strip()
        ]
        return "建议缺少完整证据引用：" + "、".join(tools) if tools else text
    if text.startswith("fixture evidence unavailable"):
        code = text.removeprefix("fixture evidence unavailable").removeprefix(":").strip()
        return "评估夹具证据不可用" + (f"（{code}）" if code else "")
    for tool_name, label in TOOL_LABELS.items():
        required_input_prefix = f"{tool_name} required input"
        if text.startswith(required_input_prefix):
            details = text.removeprefix(required_input_prefix).removeprefix(":").strip()
            return f"{label}缺少必需输入" + (f"（{details}）" if details else "")
        prefix = f"{tool_name} evidence unavailable"
        if text.startswith(prefix):
            code = text.removeprefix(prefix).removeprefix(":").strip()
            return f"{label}证据不可用" + (f"（{code}）" if code else "")
    return text


def _analysis_diagnostic_text(value: str) -> str:
    if "/" not in value and "=" in value:
        key, count = [item.strip() for item in value.split("=", 1)]
        label = DIAGNOSTIC_COUNT_LABELS.get(key, key)
        return f"分析视图诊断：{label} {count}"
    parts = [item.strip() for item in value.split("/") if item.strip()]
    if not parts:
        return "分析视图诊断异常"
    view = VIEW_LABELS.get(parts[0], parts[0])
    status = DIAGNOSTIC_STATUS_LABELS.get(parts[1], parts[1]) if len(parts) > 1 else ""
    severity = DIAGNOSTIC_SEVERITY_LABELS.get(parts[2], parts[2]) if len(parts) > 2 else ""
    details = "，".join(item for item in (status, severity) if item)
    return f"分析视图诊断：{view}" + (f"（{details}）" if details else "")


def _tool_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return TOOL_LABELS.get(text, text)


def _recommendation_heading(value: dict[str, Any]) -> str:
    action = " ".join(str(value.get("action") or "").split())
    target_scope = " ".join(str(value.get("target_scope") or "").split())
    dimension = _answer_dimension_text(value.get("answer_dimension"))
    scope = "｜".join(item for item in (dimension, target_scope) if item)
    if action and scope:
        return f"{action}（{scope}）："
    if action:
        return f"{action}："
    if scope:
        return f"{scope}："
    return ""


def _answer_dimension_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return ANSWER_DIMENSION_LABELS.get(text, text)


def _ref_text(value: Any, ref_labels: dict[str, str] | None = None) -> str:
    if not isinstance(value, list):
        return ""
    refs = [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]
    labels = ref_labels or {}
    display = [f"{ref}:{labels[ref]}" if ref in labels else ref for ref in refs]
    return f"[{', '.join(display)}] " if display else ""


def _ref_tool_labels(events: list[Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for event in events:
        if getattr(event, "type", "") != "observation":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        ref = str(payload.get("ref") or getattr(event, "visible_ref", "") or "").strip()
        tool_name = str(payload.get("tool_name") or "").strip()
        if ref and tool_name:
            labels[ref] = TOOL_LABELS.get(tool_name, tool_name)
    return labels


__all__ = ["render_user_response"]
