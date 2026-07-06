from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any

from src.application.assistant.task_contract import preview_authority_from_text, preview_request_kind_from_text
from src.application.assistant.task_profiles import TaskProfile, select_task_profiles
from src.application.assistant.time_filters import extract_month_filters


COPILOT_TASK_SCHEMA_VERSION = "om-copilot-task-v1"
COPILOT_EVIDENCE_PLAN_SCHEMA_VERSION = "om-copilot-evidence-plan-v1"


@dataclass(frozen=True)
class TaskScope:
    requested_months: tuple[str, ...] = ()
    requested_accounts: tuple[str, ...] = ()
    requested_symbols: tuple[str, ...] = ()
    requested_run_ids: tuple[str, ...] = ()
    context_mode: str = "none"

    def public_payload(self) -> dict[str, Any]:
        return {
            "requested_months": list(self.requested_months),
            "requested_accounts": list(self.requested_accounts),
            "requested_symbols": list(self.requested_symbols),
            "requested_run_ids": list(self.requested_run_ids),
            "planned_months": list(self.requested_months),
            "planned_accounts": list(self.requested_accounts),
            "planned_symbols": list(self.requested_symbols),
            "planned_run_ids": list(self.requested_run_ids),
            "context_mode": self.context_mode,
        }


@dataclass(frozen=True)
class CopilotTaskFrame:
    goal: str
    task_name: str
    task_mode: str
    domain: str
    requested_effect: str
    scope: TaskScope
    profiles: tuple[TaskProfile, ...]
    schema_version: str = COPILOT_TASK_SCHEMA_VERSION

    @property
    def required_views(self) -> tuple[str, ...]:
        return _unique_string_tuple(
            view
            for profile in self.profiles
            for view in profile.required_views
        )

    @property
    def required_answer(self) -> tuple[str, ...]:
        return _unique_string_tuple(
            answer
            for profile in self.profiles
            for answer in profile.required_answer
        )

    @property
    def answer_shape(self) -> tuple[str, ...]:
        return _unique_string_tuple(
            shape
            for profile in self.profiles
            for shape in profile.answer_shape
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "task_name": self.task_name,
            "task_mode": self.task_mode,
            "domain": self.domain,
            "requested_effect": self.requested_effect,
            "scope": self.scope.public_payload(),
            "profile_names": [profile.name for profile in self.profiles],
            "required_views": list(self.required_views),
            "required_answer": list(self.required_answer),
            "answer_shape": list(self.answer_shape),
        }

    def task_contract_payload(self) -> dict[str, Any]:
        scope = self.scope.public_payload()
        return {
            "schema_version": "om-copilot-task-contract-v1",
            "goal": self.goal,
            "domain": self.domain,
            "task_mode": self.task_mode,
            "requested_effect": self.requested_effect,
            "scope": scope,
            "required_answer": list(self.required_answer),
            "required_evidence": list(self.required_views),
            "answer_shape": list(self.answer_shape),
            "copilot_task": self.public_payload(),
            "task_profiles": [profile.name for profile in self.profiles],
        }


@dataclass(frozen=True)
class CopilotEvidenceCall:
    tool_name: str
    arguments: dict[str, Any]
    purpose: str

    def public_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class CopilotEvidencePlan:
    task_name: str
    calls: tuple[CopilotEvidenceCall, ...]
    required_views: tuple[str, ...]
    schema_version: str = COPILOT_EVIDENCE_PLAN_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_name": self.task_name,
            "calls": [call.public_payload() for call in self.calls],
            "required_views": list(self.required_views),
        }


def derive_task_frame(
    *,
    question: str,
    request_context: dict[str, Any] | None,
    today: date,
    conversation_context: dict[str, Any] | None,
) -> CopilotTaskFrame:
    text = str(question or "").strip()
    context_text = _context_text_for_followup(text, conversation_context=conversation_context)
    profile_text = "\n".join(item for item in (text, context_text) if item.strip())
    task_mode = _infer_task_mode(profile_text)
    domain = _infer_domain(profile_text)
    profiles = select_task_profiles(text=profile_text, domain=domain, task_mode=task_mode)
    primary = profiles[0] if profiles else None
    return CopilotTaskFrame(
        goal=text,
        task_name=primary.name if primary else domain,
        task_mode=task_mode,
        domain=(primary.domains[0] if primary else domain),
        requested_effect=_infer_requested_effect(text),
        scope=_derive_scope(
            question=text,
            profile_text=profile_text,
            today=today,
            conversation_context=conversation_context,
        ),
        profiles=profiles,
    )


def plan_evidence(task: CopilotTaskFrame) -> CopilotEvidencePlan:
    preview_call = _preview_call(task.goal)
    if preview_call is not None:
        return CopilotEvidencePlan(
            task_name=task.task_name,
            calls=(preview_call,),
            required_views=(),
        )
    operation_status_call = _operation_status_call(task)
    if operation_status_call is not None:
        return CopilotEvidencePlan(
            task_name="operation_status_read",
            calls=(operation_status_call,),
            required_views=("upgrade_operation_status",),
        )
    assigned_stock_call = _assigned_stock_call(task)
    if assigned_stock_call is not None:
        return CopilotEvidencePlan(
            task_name="assigned_stock_review",
            calls=(assigned_stock_call,),
            required_views=("assigned_stock_position_pnl",),
        )
    runtime_status_call = _runtime_status_call(task)
    if runtime_status_call is not None:
        return CopilotEvidencePlan(
            task_name="runtime_health_diagnosis",
            calls=(runtime_status_call,),
            required_views=("runtime_tick_status",),
        )
    calls: list[CopilotEvidenceCall] = []
    views: list[str] = []
    for profile in task.profiles:
        profile_views = _unique_string_tuple(profile.required_views)
        views.extend(profile_views)
        if profile_views:
            calls.append(
                CopilotEvidenceCall(
                    tool_name=profile.tool_name,
                    arguments=_analysis_arguments(task=task, views=profile_views),
                    purpose=f"read {profile.name} evidence",
                )
            )
    return CopilotEvidencePlan(task_name=task.task_name, calls=tuple(calls), required_views=_unique_string_tuple(views))


def compose_answer(
    *,
    task: CopilotTaskFrame,
    tool_results: tuple[dict[str, Any], ...],
) -> tuple[str, dict[str, Any]]:
    datasets = [_tool_data(result) for result in tool_results if _tool_ok(result)]
    if not datasets:
        return (
            "OM Copilot 没有拿到可用证据；本次没有形成结论。",
            {"route": "copilot_no_evidence", "successful_dataset_count": 0},
        )
    if _analysis_datasets_have_no_rows(datasets):
        return _compose_no_matching_analysis_evidence(task=task, datasets=datasets)
    if task.task_name == "option_operation_review":
        return _compose_option_operation_review(task=task, datasets=datasets)
    if task.task_name == "assigned_stock_review" or _looks_like_assigned_stock_query(task.goal):
        return _compose_assigned_stock_review(task=task, datasets=datasets)
    if task.task_name == "monthly_income_analysis":
        return _compose_monthly_income_analysis(task=task, datasets=datasets)
    return _compose_general_task(task=task, datasets=datasets)


def covered_views_from_results(tool_results: tuple[dict[str, Any], ...]) -> set[str]:
    views: set[str] = set()
    for result in tool_results:
        data = _tool_data(result)
        views.update(str(item).strip() for item in data.get("views_used") or [] if str(item).strip())
        view_datasets = data.get("view_datasets")
        if isinstance(view_datasets, dict):
            views.update(str(name).strip() for name in view_datasets if str(name).strip())
    return views


def _derive_scope(
    *,
    question: str,
    profile_text: str,
    today: date,
    conversation_context: dict[str, Any] | None,
) -> TaskScope:
    current_months = _unique_string_tuple(extract_month_filters(question, today=today))
    current_accounts = _accounts_from_text(question)
    current_symbols = _symbols_from_text(question)
    current_run_ids = _run_ids_from_text(question)
    context_mode = "none"
    months = current_months
    accounts = current_accounts
    symbols = current_symbols
    run_ids = current_run_ids
    ambiguous_context = _is_ambiguous_contextual_followup(question, conversation_context=conversation_context)
    discard_prior_context = _message_discards_prior_context(question)
    if ambiguous_context:
        context_mode = "ambiguous"
    elif _is_contextual_followup(question) and not discard_prior_context:
        if not months:
            months = _context_slot_values(conversation_context, "month")
        if not accounts:
            accounts = _context_slot_values(conversation_context, "account")
        if not symbols:
            symbols = _context_slot_values(conversation_context, "symbol")
        if not run_ids:
            run_ids = _context_slot_values(conversation_context, "run_id")
        if months or accounts or symbols or run_ids:
            context_mode = "carry"
    profile_months = tuple(extract_month_filters(profile_text, today=today))
    if not months:
        months = _unique_string_tuple(profile_months)
        if months and _is_contextual_followup(question):
            context_mode = "carry"
    return TaskScope(
        requested_months=_unique_string_tuple(months),
        requested_accounts=_unique_string_tuple(accounts),
        requested_symbols=_unique_string_tuple(symbols),
        requested_run_ids=_unique_string_tuple(run_ids),
        context_mode=context_mode,
    )


def _analysis_arguments(*, task: CopilotTaskFrame, views: tuple[str, ...]) -> dict[str, Any]:
    payload: dict[str, Any] = {"views": list(views), "limit": 200}
    months = [item for item in task.scope.requested_months if item]
    if len(months) == 1:
        payload["month"] = months[0]
    elif len(months) > 1:
        payload["months"] = months
    accounts = [item for item in task.scope.requested_accounts if item]
    if len(accounts) == 1:
        payload["account"] = accounts[0]
    elif len(accounts) > 1:
        payload["accounts"] = accounts
    symbols = [item for item in task.scope.requested_symbols if item]
    if len(symbols) == 1:
        payload["symbol"] = symbols[0]
    elif len(symbols) > 1:
        payload["symbols"] = symbols
    run_ids = [item for item in task.scope.requested_run_ids if item]
    if len(run_ids) == 1:
        payload["run_id"] = run_ids[0]
    elif len(run_ids) > 1:
        payload["run_ids"] = run_ids
    return payload


def _compose_option_operation_review(
    *,
    task: CopilotTaskFrame,
    datasets: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    rows_by_view = _rows_by_view(datasets)
    cashflow_rows = rows_by_view.get("monthly_income_cashflow_rows", [])
    exposure_rows = rows_by_view.get("open_option_exposure", [])
    performance_rows = rows_by_view.get("account_monthly_performance", [])
    income_rows = rows_by_view.get("account_monthly_income_components", [])
    trade_rows = rows_by_view.get("trade_events", [])
    months = "、".join(task.scope.requested_months) if task.scope.requested_months else "当前范围"
    assigned = [row for row in cashflow_rows if "assign" in str(row.get("trade_action") or row.get("close_type") or "").lower() or "指派" in str(row)]
    top_cash = _top_group(cashflow_rows, keys=("symbol",), value_keys=("net_cashflow_gross", "assignment_buy_cash"))
    top_exposure = _top_group(exposure_rows, keys=("symbol",), value_keys=("notional", "market_value", "cash_required", "assignment_buy_cash"))
    premium = _sum_numeric(income_rows, ("premium", "premium_received_gross", "amount_cny", "net_cashflow_gross"))
    realized = _sum_numeric(performance_rows, ("realized", "realized_gross", "net_income_cny", "net_income"))

    patterns: list[str] = []
    if top_cash:
        patterns.append(f"现金流/接货压力主要集中在 {top_cash[0]}。")
    if top_exposure:
        patterns.append(f"未平仓敞口主要集中在 {top_exposure[0]}。")
    if assigned:
        patterns.append(f"存在 {len(assigned)} 条疑似指派/接货相关记录，需要复盘开仓时的接货预算。")
    if not patterns:
        observed = len(trade_rows) or len(cashflow_rows) or len(exposure_rows)
        patterns.append(f"已读取 {observed} 条交易/敞口证据，未发现单一异常模式，但仍应按标的集中度复盘。")

    judgement = "偏保守"
    if assigned or len(top_cash) >= 2 or len(top_exposure) >= 2:
        judgement = "不够理想"
    elif premium > 0 and realized >= 0:
        judgement = "整体可接受"

    lines = [
        f"结论：{months}期权操作{judgement}，重点不是明细本身，而是接货/敞口是否被单一标的放大。",
        "问题模式：" + " ".join(patterns[:3]),
        "优化建议：下月先设单一标的最大接货资金和最大未平仓张数；卖 put 前把“最差情况接货后仓位”作为硬约束；已被指派或敞口集中的标的优先用 covered call/减仓释放现金，而不是继续加 sell put。",
        f"证据边界：基于 OM read-only analysis workspace 已读取的 {', '.join(sorted(rows_by_view))}；权利金估计={_fmt_number(premium)}，已实现/净收益估计={_fmt_number(realized)}。",
    ]
    return "\n".join(lines), {"route": "copilot_option_operation_review", "views": sorted(rows_by_view)}


def _compose_monthly_income_analysis(
    *,
    task: CopilotTaskFrame,
    datasets: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    rows_by_view = _rows_by_view(datasets)
    months = "、".join(task.scope.requested_months) if task.scope.requested_months else "当前范围"
    components = rows_by_view.get("account_monthly_income_components", [])
    attribution = rows_by_view.get("symbol_income_attribution", [])
    top_component = _top_group(components, keys=("component",), value_keys=("amount_cny", "amount", "net_income_cny"))
    top_symbol = _top_group(attribution, keys=("symbol",), value_keys=("amount_cny", "net_income_cny", "amount"))
    drivers = []
    if top_component:
        drivers.append(f"主要分项是 {top_component[0]}")
    if top_symbol:
        drivers.append(f"主要标的是 {top_symbol[0]}")
    if not drivers:
        drivers.append("当前证据能覆盖收益表，但没有足够分项可判断主要来源")
    return (
        "\n".join(
            [
                f"结论：{months}收益需要按来源拆开看，不能只看汇总行。",
                "主要来源：" + "；".join(drivers) + "。",
                "后续动作：对贡献最高的分项和标的检查是否来自一次性事件、权利金、已实现收益或汇率/正股影响。",
                f"证据边界：基于 OM read-only analysis workspace 已读取的 {', '.join(sorted(rows_by_view))}。",
            ]
        ),
        {"route": "copilot_monthly_income_analysis", "views": sorted(rows_by_view)},
    )


def _compose_general_task(
    *,
    task: CopilotTaskFrame,
    datasets: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    rows_by_view = _rows_by_view(datasets)
    row_count = sum(len(rows) for rows in rows_by_view.values())
    return (
        "\n".join(
            [
                f"结论：已按“{task.goal}”读取 OM 证据，当前可基于 {row_count} 条记录做判断。",
                f"主要证据：{', '.join(sorted(rows_by_view)) or '无明确视图'}。",
                "建议：如果要进一步优化，需要围绕异常最大的账户、标的或规则继续追问。",
                "证据边界：仅基于 OM read-only analysis workspace 本次读取结果。",
            ]
        ),
        {"route": "copilot_general", "views": sorted(rows_by_view), "row_count": row_count},
    )


def _compose_no_matching_analysis_evidence(
    *,
    task: CopilotTaskFrame,
    datasets: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    views = _analysis_views(datasets)
    diagnostics = _analysis_diagnostic_summaries(datasets)
    scope = _scope_label(task)
    boundary = "；".join(diagnostics[:2]) if diagnostics else "空结果只能说明本次查询没有匹配记录，不能证明没有问题。"
    return (
        "\n".join(
            [
                f"结论：{scope}没有匹配到可复盘的行级 OM 证据，不能判断期权操作是否不合理。",
                f"已读取：{', '.join(views) or 'OM analysis views'}；匹配到的行级记录为 0。",
                "下一步：先确认月份、市场、账户和交易/收益同步范围是否正确；拿到交易、现金流、敞口或收益行后，再给出问题模式和优化建议。",
                f"证据边界：基于 OM read-only analysis workspace；{boundary}",
            ]
        ),
        {
            "route": "copilot_no_matching_analysis_evidence",
            "row_count": 0,
            "views": list(views),
            "diagnostic_count": len(diagnostics),
        },
    )


def _rows_by_view(datasets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for data in datasets:
        view_datasets = data.get("view_datasets")
        if isinstance(view_datasets, dict):
            for name, value in view_datasets.items():
                if not isinstance(value, dict):
                    continue
                rows = [row for row in value.get("rows") or [] if isinstance(row, dict)]
                out.setdefault(str(name), []).extend(rows)
        for row in data.get("rows") or []:
            if isinstance(row, dict) and str(row.get("view") or "").strip():
                view = str(row.get("view")).strip()
                out.setdefault(view, []).append({key: value for key, value in row.items() if key != "view"})
    return out


def _analysis_datasets_have_no_rows(datasets: list[dict[str, Any]]) -> bool:
    analysis_datasets = [data for data in datasets if _is_analysis_dataset(data)]
    return bool(analysis_datasets) and sum(_analysis_row_count(data) for data in analysis_datasets) == 0


def _is_analysis_dataset(data: dict[str, Any]) -> bool:
    schema = str(data.get("schema_version") or "")
    return (
        schema.startswith("analysis.query.output")
        or isinstance(data.get("view_datasets"), dict)
        or bool(data.get("views_used"))
    )


def _analysis_row_count(data: dict[str, Any]) -> int:
    count = sum(1 for row in data.get("rows") or [] if isinstance(row, dict))
    view_datasets = data.get("view_datasets")
    if isinstance(view_datasets, dict):
        for value in view_datasets.values():
            if not isinstance(value, dict):
                continue
            count += sum(1 for row in value.get("rows") or [] if isinstance(row, dict))
    return count


def _analysis_views(datasets: list[dict[str, Any]]) -> tuple[str, ...]:
    values: list[str] = []
    for data in datasets:
        values.extend(str(item) for item in data.get("views_used") or [] if str(item).strip())
        view_datasets = data.get("view_datasets")
        if isinstance(view_datasets, dict):
            values.extend(str(name) for name in view_datasets if str(name).strip())
    return _unique_string_tuple(values)


def _analysis_diagnostic_summaries(datasets: list[dict[str, Any]]) -> tuple[str, ...]:
    summaries: list[str] = []
    for data in datasets:
        for container_key in ("query_explain", "evidence"):
            container = data.get(container_key)
            if not isinstance(container, dict):
                continue
            for item in container.get("diagnostics") or []:
                if not isinstance(item, dict):
                    continue
                summary = _localized_diagnostic_summary(
                    str(item.get("answer_boundary") or item.get("summary") or "").strip()
                )
                if summary:
                    summaries.append(summary)
    return _unique_string_tuple(summaries)


def _localized_diagnostic_summary(summary: str) -> str:
    lowered = str(summary or "").strip().lower()
    if not lowered:
        return ""
    if "cannot infer absence" in lowered:
        return "空结果不能证明没有问题。"
    if "no_matching" in lowered or "no matching" in lowered:
        return "部分证据视图没有匹配行。"
    return str(summary or "").strip()


def _scope_label(task: CopilotTaskFrame) -> str:
    parts: list[str] = []
    if task.scope.requested_months:
        parts.append("、".join(task.scope.requested_months))
    if task.scope.requested_accounts:
        parts.append("账户 " + "、".join(task.scope.requested_accounts))
    if task.scope.requested_symbols:
        parts.append("标的 " + "、".join(task.scope.requested_symbols))
    return "，".join(parts) if parts else "当前范围"


def _top_group(rows: list[dict[str, Any]], *, keys: tuple[str, ...], value_keys: tuple[str, ...]) -> list[str]:
    totals: dict[str, float] = {}
    for row in rows:
        label = " / ".join(str(row.get(key) or "").strip() for key in keys if str(row.get(key) or "").strip())
        if not label:
            continue
        value = _row_numeric(row, value_keys)
        if value == 0:
            value = 1.0
        totals[label] = totals.get(label, 0.0) + abs(value)
    return [label for label, _value in sorted(totals.items(), key=lambda item: item[1], reverse=True)[:3]]


def _sum_numeric(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> float:
    return sum(_row_numeric(row, keys) for row in rows)


def _row_numeric(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value.replace(",", ""))
            except ValueError:
                continue
    return 0.0


def _fmt_number(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _context_text_for_followup(question: str, *, conversation_context: dict[str, Any] | None) -> str:
    if not _is_contextual_followup(question):
        return ""
    if _message_discards_prior_context(question):
        return ""
    if _is_ambiguous_contextual_followup(question, conversation_context=conversation_context):
        return ""
    projection = conversation_context.get("context_projection") if isinstance(conversation_context, dict) else {}
    if not isinstance(projection, dict):
        return ""
    hints: list[str] = []
    for container_key in ("recent_turns", "recent_successful_tools", "available_evidence_refs", "open_evidence_gaps"):
        for item in reversed([entry for entry in projection.get(container_key) or [] if isinstance(entry, dict)]):
            hints.extend(_context_hint_parts(item))
    return "\n".join(hints)


def _context_hint_parts(item: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    for key in (
        "user_summary",
        "assistant_summary",
        "tool_name",
        "source_tool",
        "label",
        "purpose",
        "kind",
        "summary",
    ):
        value = str(item.get(key) or "").strip()
        if value:
            parts.append(value)
    for key in ("tools", "safe_slots", "safe_payload", "data_shape", "suggested_tools", "suggested_views"):
        value = item.get(key)
        if value not in (None, "", [], {}):
            parts.append(str(value))
    return parts


def _is_ambiguous_contextual_followup(
    question: str,
    *,
    conversation_context: dict[str, Any] | None,
) -> bool:
    compact = re.sub(r"\s+", "", str(question or "").lower())
    if compact not in {"继续", "这个", "那个", "上面", "刚才"}:
        return False
    projection = conversation_context.get("context_projection") if isinstance(conversation_context, dict) else {}
    if not isinstance(projection, dict):
        return False
    topic_markers: set[str] = set()
    for item in projection.get("recent_successful_tools") or projection.get("available_evidence_refs") or []:
        if not isinstance(item, dict):
            continue
        marker = str(item.get("tool_name") or item.get("source_tool") or "").strip()
        slots = item.get("safe_slots") if isinstance(item.get("safe_slots"), dict) else {}
        symbols = tuple(str(value) for value in slots.get("symbol") or [] if str(value).strip())
        if marker or symbols:
            topic_markers.add(f"{marker}:{symbols}")
    return len(topic_markers) > 1


def _context_slot_values(conversation_context: dict[str, Any] | None, slot_key: str) -> tuple[str, ...]:
    projection = conversation_context.get("context_projection") if isinstance(conversation_context, dict) else {}
    if not isinstance(projection, dict):
        return ()
    values: list[str] = []
    for source_name in ("recent_turns", "recent_successful_tools", "available_evidence_refs", "open_evidence_gaps"):
        for item in projection.get(source_name) or []:
            if not isinstance(item, dict):
                continue
            safe_slots = item.get("safe_slots") if isinstance(item.get("safe_slots"), dict) else {}
            values.extend(str(value) for value in safe_slots.get(slot_key) or [] if str(value).strip())
    return _unique_string_tuple(values)


def _is_contextual_followup(question: str) -> bool:
    compact = re.sub(r"\s+", "", str(question or "").lower())
    return bool(
        compact
        and len(compact) <= 24
        and any(token in compact for token in ("结论", "总结", "继续", "这个", "上面", "刚才", "这次"))
    )


def _infer_task_mode(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if any(token in compact for token in ("怎么算", "怎么计算", "如何计算", "是什么", "什么意思", "解释", "explain")):
        return "explain"
    if any(token in compact for token in ("对比", "比较", "哪个", "compare")):
        return "compare"
    if any(token in compact for token in ("分析", "复盘", "表现", "总结")):
        return "analyze"
    if any(token in compact for token in ("优化", "建议", "怎么做", "recommend")):
        return "recommend"
    if any(token in compact for token in ("为什么", "原因", "健康", "诊断", "没通过", "没有", "why")):
        return "diagnose"
    return "summarize"


def _infer_domain(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if any(token in compact for token in ("候选", "过滤", "没通过", "推荐", "太严", "sellput", "sell put", "candidate", "filter")):
        return "candidate"
    if any(token in compact for token in ("配置", "参数", "maxstrike", "minstrike", "symbol_config", "symbolstrategyconfig")):
        return "config"
    if any(token in compact for token in ("指派正股", "被指派股票", "被指派正股", "assignedstock", "assigned-stock")):
        return "position"
    if any(token in compact for token in ("收益", "收入", "权利金", "现金流", "pnl")):
        return "income"
    if any(token in compact for token in ("持仓", "仓位", "敞口", "到期", "成交", "交易记录", "写入", "开仓", "平仓")):
        return "position"
    if any(token in compact for token in ("健康", "通知", "推送", "扫描", "线上", "运行", "runtime", "升级", "更新")):
        return "runtime"
    if any(token in compact for token in ("期权操作", "期权交易", "复盘", "优化", "策略")):
        return "strategy"
    return "general"


def _infer_requested_effect(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if _looks_like_operation_status_query(text):
        return "read"
    if bool(preview_authority_from_text(text).get("allowed")):
        return "preview_write"
    if any(token in compact for token in ("改成", "改为", "设置", "新增", "删除", "升级", "更新", "记录", "写入", "补录", "apply")):
        return "preview_write"
    return "read"


def _preview_call(text: str) -> CopilotEvidenceCall | None:
    kind = preview_request_kind_from_text(text)
    kind = _concrete_preview_kind(kind=kind, text=text)
    authority = preview_authority_from_text(text)
    if kind is None:
        allowed = [str(item) for item in authority.get("allowed_preview_intents") or [] if str(item).strip()]
        if len(allowed) == 1:
            kind = allowed[0]
    if kind is None:
        return None
    arguments = _preview_arguments(kind=kind, text=text)
    return CopilotEvidenceCall(
        tool_name=kind,
        arguments=arguments,
        purpose=f"copilot preview {kind}",
    )


def _preview_arguments(*, kind: str, text: str) -> dict[str, Any]:
    if kind in {"manual_trade_open", "manual_trade_close", "manual_assignment", "manual_expiry"}:
        return _account_arguments(text)
    if kind == "monitor_run_now":
        return _monitor_run_arguments(text)
    if kind == "symbol_edit":
        return _symbol_edit_arguments(text)
    if kind == "upgrade_now":
        version = _target_version(text)
        return {"target_version": version} if version else {}
    return {}


def _concrete_preview_kind(*, kind: str | None, text: str) -> str | None:
    if kind == "manual_trade":
        return _manual_trade_preview_kind(text)
    return kind


def _manual_trade_preview_kind(text: str) -> str | None:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if not compact:
        return None
    if any(token in compact for token in ("记录开仓", "开仓", "recordopen", "open")):
        return "manual_trade_open"
    if any(token in compact for token in ("记录平仓", "平仓", "买回", "recordclose", "close")):
        return "manual_trade_close"
    if _looks_like_option_fill(compact) and "成功卖出" in compact:
        return "manual_trade_open"
    if _looks_like_option_fill(compact) and "成功买入" in compact:
        return "manual_trade_close"
    return None


def _looks_like_option_fill(compact: str) -> bool:
    return any(token in compact for token in ("成交提醒", "委托已全部成交", "$", "put", "call", "沽", "购"))


def _account_arguments(text: str) -> dict[str, Any]:
    account = _explicit_account(text)
    return {"account": account} if account else {}


def _monitor_run_arguments(text: str) -> dict[str, Any]:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    payload: dict[str, Any] = {}
    if any(token in compact for token in ("港股", "香港", "hk")):
        payload["market"] = "hk"
    elif any(token in compact for token in ("美股", "美国", "us", "usa")):
        payload["market"] = "us"
    symbols = _symbols_from_text(text)
    if symbols:
        payload["symbols"] = list(symbols)
    return payload


def _symbol_edit_arguments(text: str) -> dict[str, Any]:
    symbol = _first_symbol(text)
    field = _symbol_setting_field(text)
    value = _last_number(text)
    payload: dict[str, Any] = {}
    if symbol:
        payload["symbol"] = symbol
    if field and value is not None:
        payload["set"] = {field: value}
    return payload


def _operation_status_call(task: CopilotTaskFrame) -> CopilotEvidenceCall | None:
    if not _looks_like_operation_status_query(task.goal):
        return None
    return CopilotEvidenceCall(
        tool_name="operation_timeline",
        arguments={"operation_types": ["upgrade_now"], "limit": 5},
        purpose="read recent upgrade operation status",
    )


def _looks_like_operation_status_query(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if not compact:
        return False
    status_word = any(token in compact for token in ("了吗", "是否", "有没有", "成功了吗", "状态", "结果"))
    operation_word = any(token in compact for token in ("立即更新", "升级", "更新", "upgrade", "release"))
    return status_word and operation_word


def _runtime_status_call(task: CopilotTaskFrame) -> CopilotEvidenceCall | None:
    if task.task_name != "runtime_health_diagnosis":
        return None
    run_id = _first_string(task.scope.requested_run_ids)
    if not run_id:
        return None
    return CopilotEvidenceCall(
        tool_name="runtime_status",
        arguments={"run_id": run_id},
        purpose="read carried runtime status",
    )


def _assigned_stock_call(task: CopilotTaskFrame) -> CopilotEvidenceCall | None:
    if not _looks_like_assigned_stock_query(task.goal):
        return None
    payload: dict[str, Any] = {
        "action": "assigned-stock",
        "status": "open",
        "refresh_quotes": True,
    }
    account = _first_string(task.scope.requested_accounts)
    if account:
        payload["account"] = account
    symbol = _first_string(task.scope.requested_symbols)
    if symbol:
        payload["symbol"] = symbol
    return CopilotEvidenceCall(
        tool_name="option_positions_read",
        arguments=payload,
        purpose="read assigned stock lifecycle PnL",
    )


def _looks_like_assigned_stock_query(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if not compact:
        return False
    assigned = any(token in compact for token in ("指派正股", "被指派股票", "被指派正股", "assignedstock", "assigned-stock"))
    pnl = any(token in compact for token in ("收益", "盈亏", "pnl", "浮盈", "浮亏", "成本", "生命周期"))
    return assigned and pnl


def _explicit_account(text: str) -> str | None:
    accounts = _accounts_from_text(text)
    return accounts[0] if accounts else None


def _accounts_from_text(text: str) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    accounts: list[str] = []
    for account in ("lx", "sy"):
        if re.search(rf"(?<![a-z0-9]){account}(?![a-z])", compact):
            accounts.append(account)
    return tuple(accounts)


def _run_ids_from_text(text: str) -> tuple[str, ...]:
    return _unique_string_tuple(
        match.group(0)
        for match in re.finditer(r"(?<![A-Za-z0-9_-])(?:us|hk)-20\d{6}(?:[A-Za-z0-9_-]*)?(?![A-Za-z0-9_-])", str(text or ""), re.IGNORECASE)
    )


def _first_string(values: tuple[str, ...]) -> str | None:
    return values[0] if values else None


def _symbols_from_text(text: str) -> tuple[str, ...]:
    focus_text = _symbol_focus_text(text)
    symbols = [_normalize_symbol(match.group(0)) for match in _symbol_matches(focus_text)]
    return _unique_string_tuple(symbol for symbol in symbols if symbol)


def _first_symbol(text: str) -> str | None:
    for match in _symbol_matches(_symbol_focus_text(text)):
        symbol = _normalize_symbol(match.group(0))
        if symbol:
            return symbol
    return None


def _symbol_matches(text: str) -> list[re.Match[str]]:
    return list(re.finditer(r"(?<![A-Za-z0-9_.])(?:[A-Za-z]{2,8}(?:\.[A-Za-z]{1,4})?|\d{3,5}(?:\.HK)?)(?![A-Za-z0-9_.年])", str(text or "")))


def _symbol_focus_text(text: str) -> str:
    value = str(text or "")
    for token in ("先放下", "放下", "先不看", "不用看"):
        if token in value:
            suffix = value.split(token, 1)[1]
            if suffix.strip():
                return suffix
    return value


def _message_discards_prior_context(text: str) -> bool:
    return any(token in str(text or "") for token in ("先放下", "放下", "先不看", "不用看"))


def _normalize_symbol(raw: str) -> str:
    symbol = str(raw or "").strip().upper()
    if not symbol or symbol in {"SELL", "PUT", "CALL", "MAX", "STRIKE", "MONITOR", "RUN", "HK", "US", "LX", "SY"}:
        return ""
    if re.fullmatch(r"\d{3,5}", symbol):
        return f"{symbol}.HK"
    return symbol


def _symbol_setting_field(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or "").lower()).replace("_", "")
    strategy = "sell_put" if "sellput" in compact or "put" in compact else ""
    if "maxstrike" in compact or "最高行权价" in compact:
        return f"{strategy or 'sell_put'}.max_strike"
    if "minstrike" in compact or "最低行权价" in compact:
        return f"{strategy or 'sell_call'}.min_strike"
    if "enabled" in compact or "启用" in compact:
        return f"{strategy or 'sell_put'}.enabled"
    return ""


def _last_number(text: str) -> int | float | None:
    matches = re.findall(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![A-Za-z0-9.])", str(text or ""))
    if not matches:
        return None
    raw = matches[-1]
    value = float(raw)
    return int(value) if value.is_integer() else value


def _target_version(text: str) -> str | None:
    match = re.search(r"v?(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?)", str(text or ""))
    return match.group(1) if match else None


def _compose_assigned_stock_review(
    *,
    task: CopilotTaskFrame,
    datasets: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    rows = [
        row
        for data in datasets
        for row in (data.get("rows") or data.get("assigned_stock_lots") or [])
        if isinstance(row, dict)
    ]
    if not rows:
        return (
            "结论：当前没有读到未卖出的指派正股记录，无法计算持仓收益。",
            {"route": "copilot_assigned_stock_review", "row_count": 0},
        )
    unrealized = _sum_numeric(rows, ("assigned_stock_unrealized_pnl",))
    realized = _sum_numeric(rows, ("assigned_stock_realized_pnl",))
    lifecycle = _sum_numeric(rows, ("assignment_lifecycle_pnl",))
    symbols = _unique_string_tuple(row.get("symbol") for row in rows)
    return (
        "\n".join(
            [
                f"结论：当前有 {len(rows)} 条指派正股记录，主要标的：{', '.join(symbols) or '未标明'}。",
                f"收益口径：正股浮盈亏约 {_fmt_number(unrealized)}，正股已实现约 {_fmt_number(realized)}，生命周期 PnL 约 {_fmt_number(lifecycle)}。",
                "优化建议：优先检查仍未卖出的指派正股是否占用过多现金；若标的基本面和价格不支持继续持有，应先用 covered call 或减仓释放资金，再决定是否继续卖 put。",
                "证据边界：基于 OM 本地 assigned_stock_events + trade_events 只读结果；报价缺失时浮盈亏可能不完整。",
            ]
        ),
        {"route": "copilot_assigned_stock_review", "row_count": len(rows)},
    )


def _tool_ok(result: dict[str, Any]) -> bool:
    return isinstance(result, dict) and bool(result.get("ok"))


def _tool_data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") if isinstance(result, dict) else {}
    return data if isinstance(data, dict) else {}


def _unique_string_tuple(values: Any) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


__all__ = [
    "COPILOT_EVIDENCE_PLAN_SCHEMA_VERSION",
    "COPILOT_TASK_SCHEMA_VERSION",
    "CopilotEvidenceCall",
    "CopilotEvidencePlan",
    "CopilotTaskFrame",
    "TaskScope",
    "compose_answer",
    "covered_views_from_results",
    "derive_task_frame",
    "plan_evidence",
]
