from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.application.agent_tool_registry import get_tool_definition
from src.application.copilot.contracts import safe_error_code
from src.application.tool_execution import execute_tool

MAX_TOOL_DESCRIPTION_CHARS = 280
MAX_TOOL_FIELD_CHARS = 80
MAX_OUTPUT_CONTRACT_FIELDS = 12
MAX_FACTS = 28
MAX_FACT_CHARS = 260
MAX_ROW_FACT_FIELDS = 8
MAX_ROW_FACTS_PER_GROUP = 4
MAX_TAIL_SAMPLE_FACTS = 2
VALID_CURRENT_NEGATIVE_EVIDENCE = "valid_current_negative_evidence"
VALID_REQUESTED_PERIOD_NEGATIVE_EVIDENCE = "valid_requested_period_negative_evidence"
VALID_EMPTY_RESULT_MEANINGS_BY_VIEW = {
    "open_option_exposure": {VALID_CURRENT_NEGATIVE_EVIDENCE},
    "expiration_risk_buckets": {VALID_CURRENT_NEGATIVE_EVIDENCE},
    "trade_events": {VALID_REQUESTED_PERIOD_NEGATIVE_EVIDENCE},
}
_PROBLEM_DIAGNOSTIC_COUNT_KEYS = ("warning_count", "missing_view_count", "stale_view_count")


@dataclass(frozen=True)
class AgentToolView:
    name: str
    required_scene_fields: tuple[str, ...] = ()
    payload_fields: dict[str, str] = field(default_factory=dict)
    claimable: bool = True
    evidence_context: dict[str, str] = field(default_factory=dict)
    observation_evidence_context: Any = None
    observation_summary: Any = None
    observation_facts: Any = None
    evidence_available: Any = None
    missing_evidence: Any = None


def _default_summary(tool_name: str, data: dict[str, Any]) -> str:
    return f"{tool_name} returned read-only data with keys: {', '.join(sorted(data)[:8])}"


def _default_evidence_ok(data: dict[str, Any]) -> bool:
    return bool(data)


def _default_missing_data(_data: dict[str, Any], *, evidence_ok: bool) -> list[str]:
    return [] if evidence_ok else ["tool evidence"]


def _compact_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _compact_string_list(values: Any) -> list[str]:
    if isinstance(values, dict):
        items = values.keys()
    elif isinstance(values, (list, tuple, set, frozenset)):
        items = values
    else:
        return []
    result: list[str] = []
    for value in items:
        text = _fact_nested_value(value) if isinstance(value, (dict, list, tuple, set)) else _compact_text(value, MAX_TOOL_FIELD_CHARS)
        if text:
            result.append(text)
    return result


def _compact_string_map(values: Any) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in values.items():
        key_text = _compact_text(key, MAX_TOOL_FIELD_CHARS)
        value_text = _fact_nested_value(value) if isinstance(value, (dict, list, tuple, set)) else _compact_text(value, MAX_TOOL_FIELD_CHARS)
        if key_text and value_text:
            result[key_text] = value_text
    return result


def build_tool_payload(
    tool_name: str,
    scene_input: dict[str, Any],
    *,
    static_payloads: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    view = TOOL_VIEWS.get(tool_name)
    if view is None:
        return None, f"unsupported copilot tool: {tool_name}"

    payload: dict[str, Any] = {}
    scene_payload = (static_payloads or {}).get(tool_name)
    if isinstance(scene_payload, dict):
        payload.update(scene_payload)
    for source_field, payload_field in view.payload_fields.items():
        value = _scene_string(scene_input, source_field)
        if value:
            payload[payload_field] = value
    for field_name in view.required_scene_fields:
        if not _scene_string(scene_input, field_name):
            return None, f"{tool_name} requires {field_name}"
    return payload, None


def call_read_tool(tool_name: str, payload: dict[str, Any], *, allowed_tools: tuple[str, ...]) -> dict[str, Any]:
    if tool_name not in allowed_tools:
        return _tool_error(tool_name, "POLICY_ERROR", f"tool is not allowed by scene manifest: {tool_name}")
    definition = get_tool_definition(tool_name)
    if definition is None:
        return _tool_error(tool_name, "INPUT_ERROR", f"unknown tool: {tool_name}")
    if not definition.is_pure_read():
        return _tool_error(tool_name, "POLICY_ERROR", f"tool is not pure read-only: {tool_name}")
    return execute_tool(tool_name, payload)


def tool_descriptions(
    tool_names: list[str] | tuple[str, ...],
    *,
    static_payloads: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    descriptions: list[dict[str, Any]] = []
    for tool_name in tool_names:
        view = TOOL_VIEWS.get(tool_name)
        definition = get_tool_definition(tool_name)
        if view is None or definition is None or not definition.is_pure_read():
            continue
        scene_payload = (static_payloads or {}).get(tool_name)
        description_payload = _description_payload(view, scene_payload)
        descriptions.append(
            {
                "name": tool_name,
                "description": _compact_text(definition.description, MAX_TOOL_DESCRIPTION_CHARS),
                "capabilities": _compact_string_list(definition.capabilities),
                "required_scene_fields": list(view.required_scene_fields),
                "payload_fields": dict(view.payload_fields),
                "evidence_context": _compact_string_map(view.evidence_context),
                "input_fields": _compact_string_list(definition.input_schema),
                "output_contract": _resolved_output_contract_preview(definition, description_payload),
            }
        )
    return descriptions


def compact_observation(tool_name: str, response: dict[str, Any]) -> dict[str, Any]:
    view = TOOL_VIEWS.get(tool_name)
    ok = bool(response.get("ok"))
    if not ok:
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        error_code = safe_error_code(error.get("code"), default="TOOL_ERROR")
        return {
            "tool_name": tool_name,
            "ok": False,
            "summary": f"{tool_name} failed with code {error_code}.",
            "value_preview": _value_preview(response.get("data") or {}),
            "error": {"code": error_code, "has_message": "message" in error and bool(str(error["message"]).strip())},
            "evidence_ok": False,
            "claimable": False,
            "evidence_context": _compact_evidence_context(view, {}),
            "missing_data": [f"{tool_name} evidence unavailable: {error_code}"],
        }

    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    evidence_ok = _evidence_ok(view, data)
    facts, facts_omitted = _compact_facts(tool_name, data)
    observation = {
        "tool_name": tool_name,
        "ok": True,
        "summary": _compact_summary(tool_name, data, response.get("warnings")),
        "facts": facts,
        "value_preview": _value_preview(data),
        "error": None,
        "evidence_ok": evidence_ok,
        "claimable": bool(view.claimable) if view else True,
        "evidence_context": _compact_evidence_context(view, data),
        "missing_data": _missing_data(view, data, evidence_ok=evidence_ok),
    }
    if facts_omitted:
        observation["facts_omitted"] = facts_omitted
    return observation


def _tool_error(tool_name: str, code: str, message: str) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def _scene_string(scene_input: dict[str, Any], field_name: str) -> str:
    value = scene_input.get(field_name)
    return value.strip() if isinstance(value, str) else ""


def _description_payload(view: AgentToolView, scene_payload: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if isinstance(scene_payload, dict):
        payload.update(scene_payload)
    return payload


def _resolved_output_contract_preview(definition: Any, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        contract = definition.resolve_output_contract(payload)
    except Exception:
        return {}
    return _output_contract_preview(contract)


def _output_contract_preview(contract: Any) -> dict[str, Any]:
    if not isinstance(contract, dict):
        return {}
    preview: dict[str, Any] = {}
    for key in (
        "schema_version",
        "canonical_renderer",
        "source_label",
        "guard_profile",
        "answer_surface",
        "primary_rows",
        "row_count_field",
        "stable_order",
        "payload_dependent",
    ):
        value = _contract_scalar(contract.get(key))
        if value is not None:
            preview[key] = value
    for key in (
        "fact_fields",
        "freshness_fields",
        "missing_data_fields",
        "calculation_fields",
        "model_preview_fields",
    ):
        values = _compact_string_list(contract.get(key))[:MAX_OUTPUT_CONTRACT_FIELDS]
        if values:
            preview[key] = values
    return preview


def _contract_scalar(value: Any) -> str | bool | int | float | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = _compact_text(value, MAX_TOOL_FIELD_CHARS)
        return text or None
    return None


def _compact_summary(tool_name: str, data: dict[str, Any], warnings: Any) -> str:
    warning_text = _warning_text(warnings)
    view = TOOL_VIEWS.get(tool_name)
    observation_summary = view.observation_summary if view and view.observation_summary else None
    base = observation_summary(data) if observation_summary else _default_summary(tool_name, data)
    return f"{base} Warnings: {warning_text}" if warning_text else base


def _compact_facts(tool_name: str, data: dict[str, Any]) -> tuple[list[str], int]:
    view = TOOL_VIEWS.get(tool_name)
    builder = view.observation_facts if view and view.observation_facts else None
    raw_facts = builder(data) if builder else []
    compacted: list[str] = []
    for item in raw_facts:
        text = _compact_text(item, MAX_FACT_CHARS)
        if text:
            compacted.append(text)
    return _bounded_facts(compacted)


def _bounded_facts(facts: list[str]) -> tuple[list[str], int]:
    if len(facts) <= MAX_FACTS:
        return facts, 0
    samples = [fact for fact in facts if _sample_fact(fact)]
    selected = [fact for fact in facts if not _sample_fact(fact)][:MAX_FACTS]
    tail_samples = samples[-MAX_TAIL_SAMPLE_FACTS:]
    for fact in tail_samples:
        if fact not in selected and len(selected) < MAX_FACTS:
            selected.append(fact)
    for fact in facts:
        if len(selected) >= MAX_FACTS:
            break
        if fact not in selected:
            selected.append(fact)
    return selected, len(facts) - MAX_FACTS


def _sample_fact(fact: str) -> bool:
    label = fact.split(":", 1)[0]
    if label.startswith(("diagnostic[", "freshness[")):
        return False
    return "[" in label or label.endswith(".remaining_rows")


def _evidence_ok(view: AgentToolView | None, data: dict[str, Any]) -> bool:
    checker = view.evidence_available if view and view.evidence_available else None
    return bool(checker(data)) if checker else _default_evidence_ok(data)


def _missing_data(view: AgentToolView | None, data: dict[str, Any], *, evidence_ok: bool) -> list[str]:
    finder = view.missing_evidence if view and view.missing_evidence else None
    return list(finder(data, evidence_ok=evidence_ok)) if finder else _default_missing_data(data, evidence_ok=evidence_ok)


def _compact_evidence_context(view: AgentToolView | None, data: dict[str, Any]) -> dict[str, str]:
    if view is None:
        return {}
    builder = view.observation_evidence_context
    context = builder(data) if builder else view.evidence_context
    compacted = _compact_string_map(context)
    if not isinstance(context, dict):
        return compacted
    return compacted


def _compact_candidate_filter(data: dict[str, Any]) -> str:
    symbol = str(data.get("canonical_symbol") or data.get("symbol") or "symbol")
    trace_count = int(data.get("trace_count") or 0)
    if trace_count <= 0:
        return f"candidate_filter_explain found no trace rows for {symbol}; exact filter reason is unavailable."

    fragments: list[str] = []
    for item in data.get("functions") or []:
        if not isinstance(item, dict):
            continue
        function = str(item.get("function") or "unknown")
        status = str(item.get("status") or "unknown")
        rejections = item.get("rejection_reasons") or []
        if rejections and isinstance(rejections[0], dict):
            top = rejections[0]
            label = top.get("label") or top.get("rule")
            fragments.append(f"{function}: {status}, top rejection={label}")
        else:
            fragments.append(f"{function}: {status}")
    detail = "; ".join(fragments[:4]) if fragments else f"status_counts={data.get('status_counts') or {}}"
    return f"candidate_filter_explain found {trace_count} trace rows for {symbol}: {detail}."


def _candidate_filter_facts(data: dict[str, Any]) -> list[str]:
    symbol = str(data.get("canonical_symbol") or data.get("symbol") or "symbol")
    facts = [f"symbol={symbol}", f"trace_count={int(data.get('trace_count') or 0)}"]
    for item in data.get("functions") or []:
        if not isinstance(item, dict):
            continue
        facts.append(_mapping_fact("function", item, ("function", "status", "rejection_count")))
        reasons = item.get("rejection_reasons")
        if isinstance(reasons, list):
            facts.extend(_row_facts("rejection", reasons, ("label", "rule", "count"), limit=2))
    return facts


def _compact_runtime_status(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("status", "overall_status", "runtime_status"):
        value = data.get(key)
        if value:
            parts.append(f"{key}={value}")
            break
    freshness = data.get("freshness")
    if isinstance(freshness, dict):
        fresh_status = freshness.get("status") or freshness.get("overall_status")
        if fresh_status:
            parts.append(f"freshness={fresh_status}")
    latest = data.get("latest_run") or data.get("last_run") or data.get("run")
    if isinstance(latest, dict):
        run_id = latest.get("run_id") or latest.get("id") or latest.get("name")
        if run_id:
            parts.append(f"latest_run={run_id}")
    notification = data.get("notification_diagnosis")
    if isinstance(notification, dict):
        status = notification.get("status")
        if status:
            parts.append(f"notification_status={status}")
        reason = notification.get("reason")
        if reason:
            parts.append(f"notification_reason={_compact_text(reason, MAX_TOOL_FIELD_CHARS)}")
    if not parts:
        keys = ", ".join(sorted(data)[:8])
        parts.append(f"returned keys: {keys}")
    return "runtime_status " + "; ".join(parts) + "."


def _runtime_status_facts(data: dict[str, Any]) -> list[str]:
    facts = []
    for key in ("status", "overall_status", "runtime_status"):
        if data.get(key):
            facts.append(f"{key}={data.get(key)}")
    freshness = data.get("freshness")
    if isinstance(freshness, dict):
        facts.append(_mapping_fact("freshness", freshness, ("status", "overall_status", "latest_run_age_seconds")))
    latest = data.get("latest_run") or data.get("last_run") or data.get("run")
    if isinstance(latest, dict):
        facts.append(_mapping_fact("latest_run", latest, ("run_id", "id", "status", "market", "ended_at")))
    notification = data.get("notification_diagnosis")
    if isinstance(notification, dict):
        facts.append(
            _mapping_fact(
                "notification_diagnosis",
                notification,
                (
                    "status",
                    "reason",
                    "scheduler_should_run_scan",
                    "scheduler_should_notify",
                    "scheduler_reason",
                    "no_send",
                    "account_messages_count",
                    "send_attempted_count",
                    "send_confirmed_count",
                    "send_failed_count",
                    "final_reason",
                ),
            )
        )
        route = notification.get("notification_route")
        if isinstance(route, dict):
            facts.append(_mapping_fact("notification_route", route, ("configured", "provider", "channel", "target_configured")))
    return facts


def _compact_analysis_catalog(data: dict[str, Any]) -> str:
    view_names = data.get("view_names")
    if not isinstance(view_names, list):
        views = data.get("views")
        view_names = sorted(str(key) for key in views) if isinstance(views, dict) else []
    fragments = [
        f"view_count={int(data.get('view_count') or len(view_names))}",
    ]
    if view_names:
        fragments.append(f"views={', '.join(str(name) for name in view_names[:8])}")
    sql_rules = data.get("sql_rules")
    if isinstance(sql_rules, dict):
        fragments.append(f"writes_allowed={bool(sql_rules.get('writes_allowed'))}")
        statements = sql_rules.get("allowed_statements")
        if isinstance(statements, list):
            fragments.append("allowed_statements=" + ",".join(map(str, statements[:3])))
    return "analysis_catalog " + "; ".join(fragments) + "."


def _analysis_catalog_facts(data: dict[str, Any]) -> list[str]:
    facts = [f"view_count={int(data.get('view_count') or 0)}"]
    view_names = data.get("view_names")
    if isinstance(view_names, list):
        facts.append("views=" + ", ".join(str(name) for name in view_names[:12]))
    sql_rules = data.get("sql_rules")
    if isinstance(sql_rules, dict):
        facts.append(_mapping_fact("sql_rules", sql_rules, ("writes_allowed", "single_statement_only", "max_limit")))
    return facts


def _compact_analysis_query(data: dict[str, Any]) -> str:
    views_used = data.get("views_used") if isinstance(data.get("views_used"), list) else []
    row_count = _analysis_row_count(data)
    fragments = [
        f"row_count={row_count}",
    ]
    query = data.get("query")
    if isinstance(query, dict):
        mode = query.get("mode")
        if mode:
            fragments.append(f"mode={mode}")
    if views_used:
        fragments.append("views_used=" + ", ".join(str(view) for view in views_used[:8]))
    coverage = _analysis_coverage(data)
    if coverage:
        fragments.append("coverage=" + coverage)
    diagnostics = _analysis_diagnostics(data)
    if diagnostics:
        fragments.append("diagnostics=" + diagnostics)
    return "analysis_query " + "; ".join(fragments) + "."


def _analysis_query_missing_evidence(data: dict[str, Any], *, evidence_ok: bool) -> list[str]:
    missing: list[str] = []
    missing.extend(_empty_filtered_view_items(data))
    if evidence_ok:
        return _dedupe(missing)
    if _analysis_row_count(data) <= 0:
        missing.append("analysis_query rows")
    missing.extend(_diagnostic_missing_items("analysis_query", data.get("evidence")))
    missing.extend(_diagnostic_missing_items("analysis_query", data.get("query_explain")))
    return _dedupe(missing)


def _analysis_query_facts(data: dict[str, Any]) -> list[str]:
    facts = [f"row_count={_analysis_row_count(data)}"]
    query = data.get("query")
    if isinstance(query, dict):
        filters = query.get("filters")
        if isinstance(filters, dict):
            filter_fact = _mapping_fact("query_filters", filters, ("months", "accounts", "symbols"))
            if filter_fact:
                facts.append(filter_fact)
    views_used = data.get("views_used")
    if isinstance(views_used, list):
        facts.append("views_used=" + ", ".join(str(view) for view in views_used[:12]))
    evidence = data.get("evidence")
    if isinstance(evidence, dict):
        coverage = evidence.get("coverage")
        if isinstance(coverage, dict):
            facts.append(
                _mapping_fact(
                    "coverage",
                    coverage,
                    (
                        "row_count",
                        "month_count",
                        "account_count",
                        "symbol_count",
                        "views",
                        "months",
                        "accounts",
                        "symbols",
                        "currencies",
                    ),
                )
            )
        diagnostics = evidence.get("diagnostics")
        if isinstance(diagnostics, list):
            facts.extend(_row_facts("diagnostic", diagnostics, ("view", "status", "severity"), limit=3))
        freshness = evidence.get("freshness")
        if isinstance(freshness, list):
            facts.extend(_row_facts("freshness", freshness, ("view", "freshness", "status"), limit=4))
    view_datasets = data.get("view_datasets")
    if isinstance(view_datasets, dict):
        view_items: list[tuple[str, list[Any]]] = []
        for view_name in _ordered_view_dataset_names(data):
            dataset = view_datasets.get(view_name)
            if not isinstance(dataset, dict):
                continue
            facts.extend(_analysis_view_context_facts(view_name, dataset))
            facts.append(f"{view_name}.row_count={int(dataset.get('row_count') or _list_count(dataset, 'rows'))}")
            rows = dataset.get("rows")
            if isinstance(rows, list):
                view_items.append((view_name, rows))
                facts.extend(_analysis_view_aggregate_facts(view_name, rows))
        for view_name, rows in view_items:
            facts.extend(
                _row_facts(
                    view_name,
                    rows,
                    (
                        "month",
                        "account",
                        "symbol",
                        "close_action",
                        "tier",
                        "reason",
                        "currency",
                        "component",
                        "position_effect",
                        "side",
                        "option_type",
                        "contracts",
                        "price",
                        "strike",
                        "expiration_ymd",
                        "trade_time_beijing",
                        "net_income_cny",
                        "premium_income_cny",
                        "realized_pnl_cny",
                        "amount_gross",
                        "contracts_open",
                        "cash_secured_amount",
                    ),
                    limit=2,
                )
            )
    return [fact for fact in facts if fact]


def _analysis_view_aggregate_facts(view_name: str, rows: list[Any]) -> list[str]:
    facts: list[str] = []
    if _rows_have_any(rows, ("net_income_cny", "premium_income_cny", "realized_pnl_cny", "cash_secured_cny")):
        facts.extend(
            _numeric_total_facts(
                view_name,
                rows,
                ("net_income_cny", "premium_income_cny", "realized_pnl_cny", "cash_secured_cny"),
            )
        )
    if _rows_have_all(rows, ("component", "amount_cny")):
        facts.extend(_group_sum_facts(view_name, rows, group_fields=("component",), sum_fields=("amount_cny",)))
    if _rows_have_all(rows, ("symbol", "currency", "component", "amount_gross")):
        facts.extend(
            _group_sum_facts(
                view_name,
                rows,
                group_fields=("symbol", "currency", "component"),
                sum_fields=("amount_gross",),
            )
        )
    if _rows_have_all(rows, ("symbol", "currency")) and _rows_have_any(rows, ("contracts_open", "cash_secured_amount")):
        facts.extend(
            _group_sum_facts(
                view_name,
                rows,
                group_fields=("symbol", "currency"),
                sum_fields=("contracts_open", "cash_secured_amount"),
            )
        )
    if _rows_have_all(rows, ("symbol", "currency", "option_type", "side")) and _rows_have_any(
        rows,
        ("contracts_open", "cash_secured_amount"),
    ):
        facts.extend(
            _group_sum_facts(
                view_name,
                rows,
                group_fields=("symbol", "currency", "option_type", "side"),
                sum_fields=("contracts_open", "cash_secured_amount"),
            )
        )
    if _rows_have_all(rows, ("symbol", "currency", "position_effect", "side")) and _rows_have_any(rows, ("contracts",)):
        facts.extend(
            _group_sum_facts(
                view_name,
                rows,
                group_fields=("symbol", "currency", "position_effect", "side"),
                sum_fields=("contracts",),
            )
        )
    if _rows_have_all(rows, ("expiration_bucket", "currency")):
        facts.extend(
            _group_sum_facts(
                view_name,
                rows,
                group_fields=("expiration_bucket", "currency"),
                sum_fields=("position_count", "contracts_open", "cash_secured_amount"),
            )
        )
    return facts[:5]


def _analysis_view_context_facts(view_name: str, dataset: dict[str, Any]) -> list[str]:
    facts: list[str] = []
    if _empty_view_dataset_is_valid_negative_evidence(view_name, dataset):
        facts.append(f"{view_name}.empty_result_meaning={dataset.get('empty_result_meaning')}")
    return facts


def _analysis_query_evidence_context(data: dict[str, Any]) -> dict[str, str]:
    context = {
        "time_scope": "requested_filters",
        "record_type": "approved_analysis_view_rows",
        "use_as": "materialized analysis cross-check evidence",
    }
    snapshot_views = _analysis_snapshot_view_names(data)
    if snapshot_views:
        context["snapshot_views"] = ",".join(snapshot_views)
        context["snapshot_note"] = "snapshot views are current/latest context, not requested-month transaction history"
    empty_views, empty_meanings = _analysis_valid_empty_views_and_meanings(data)
    if empty_views:
        context["valid_empty_result_views"] = ",".join(empty_views)
        context["valid_empty_result_meanings"] = ",".join(empty_meanings)
        context["valid_empty_result_note"] = "empty result meaning is valid only for the listed view semantics"
    return context


def _analysis_snapshot_view_names(data: dict[str, Any]) -> list[str]:
    view_datasets = data.get("view_datasets")
    if not isinstance(view_datasets, dict):
        return []
    names: list[str] = []
    for view_name in _ordered_view_dataset_names(data):
        if view_name in {"open_option_exposure", "expiration_risk_buckets", "close_advice_snapshot"}:
            names.append(view_name)
    return names


def _analysis_valid_empty_views_and_meanings(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    view_datasets = data.get("view_datasets")
    if not isinstance(view_datasets, dict):
        return [], []
    views: list[str] = []
    meanings: list[str] = []
    for view_name in _ordered_view_dataset_names(data):
        dataset = view_datasets.get(view_name)
        if not isinstance(dataset, dict) or not _empty_view_dataset_is_valid_negative_evidence(view_name, dataset):
            continue
        views.append(view_name)
        meaning = str(dataset.get("empty_result_meaning") or "")
        if meaning not in meanings:
            meanings.append(meaning)
    return views, meanings


def _ordered_view_dataset_names(data: dict[str, Any]) -> list[str]:
    view_datasets = data.get("view_datasets")
    if not isinstance(view_datasets, dict):
        return []
    names: list[str] = []
    views_used = data.get("views_used")
    if isinstance(views_used, list):
        for item in views_used:
            name = str(item)
            if name in view_datasets and name not in names:
                names.append(name)
    for name in sorted(str(item) for item in view_datasets):
        if name not in names:
            names.append(name)
    return names[:8]


def _compact_monthly_income(data: dict[str, Any]) -> str:
    return_summary_count = _list_count(data, "return_summary")
    summary_count = _list_count(data, "summary")
    rows_count = _list_count(data, "rows")
    cashflow_count = _list_count(data, "cashflow_rows") or int(data.get("row_count") or 0)
    premium_count = _list_count(data, "premium_rows") or int(data.get("premium_row_count") or 0)
    realized_count = _list_count(data, "realized_rows")
    assignment_count = _list_count(data, "assignment_lifecycle_rows")
    enhancement_count = _list_count(data, "enhancement_rows")
    if max(
        return_summary_count,
        summary_count,
        rows_count,
        cashflow_count,
        premium_count,
        realized_count,
        assignment_count,
        enhancement_count,
    ) <= 0:
        return "monthly_income_report returned no monthly income rows for the requested scope."

    fragments = [
        f"return_summary_rows={return_summary_count}",
        f"summary_rows={summary_count}",
        f"rows={rows_count}",
        f"cashflow_rows={cashflow_count}",
        f"premium_rows={premium_count}",
        f"realized_rows={realized_count}",
        f"assignment_lifecycle_rows={assignment_count}",
        f"enhancement_rows={enhancement_count}",
    ]
    return "monthly_income_report " + "; ".join(fragments) + "."


def _monthly_income_missing_evidence(data: dict[str, Any], *, evidence_ok: bool) -> list[str]:
    if evidence_ok:
        return []
    missing = ["monthly_income_report evidence"]
    if _list_count(data, "return_summary") > 0:
        missing.append("monthly_income_report detail rows")
    diagnostics = data.get("diagnostics")
    if isinstance(diagnostics, list):
        for item in diagnostics[:3]:
            if not isinstance(item, dict):
                continue
            fields = _compact_string_list(item.get("missing_fields"))
            matched_trade_events_count = item.get("matched_trade_events_count")
            no_matched_trade_events = (
                matched_trade_events_count is not None
                and not isinstance(matched_trade_events_count, bool)
                and _int_count(matched_trade_events_count) == 0
            )
            if fields:
                if no_matched_trade_events:
                    fields = [field for field in fields if field != "trade_events"]
                if fields:
                    missing.append("monthly_income_report missing fields: " + ", ".join(fields[:8]))
            if no_matched_trade_events:
                missing.append("monthly_income_report no matched trade_events")
            status = _compact_text(item.get("status"), 40)
            if status:
                missing.append(f"monthly_income_report diagnostic status: {status}")
    return _dedupe(missing)


def _monthly_income_facts(data: dict[str, Any]) -> list[str]:
    return_summary = data.get("return_summary")
    summary_rows = data.get("summary")
    cashflow_rows = data.get("cashflow_rows")
    premium_rows = data.get("premium_rows")
    realized_rows = data.get("realized_rows")
    assignment_rows = data.get("assignment_lifecycle_rows")
    enhancement_rows = data.get("enhancement_rows")
    facts = [
        f"return_summary_rows={_list_count(data, 'return_summary')}",
        f"summary_rows={_list_count(data, 'summary')}",
        f"premium_rows={_list_count(data, 'premium_rows')}",
        f"realized_rows={_list_count(data, 'realized_rows')}",
        f"assignment_lifecycle_rows={_list_count(data, 'assignment_lifecycle_rows')}",
    ]
    if _list_count(data, "enhancement_rows") > 0:
        facts.append(f"enhancement_rows={_list_count(data, 'enhancement_rows')}")
    facts.extend(
        _numeric_total_facts(
            "return_summary",
            return_summary,
            ("net_income_cny", "premium_income_cny", "realized_pnl_cny", "cash_secured_cny"),
        )
    )
    facts.extend(
        _group_sum_facts(
            "summary",
            summary_rows,
            group_fields=("currency",),
            sum_fields=(
                "net_cashflow_gross",
                "premium_received_gross",
                "realized_gross",
                "assignment_stock_net_cashflow_gross",
                "premium_contracts",
                "closed_contracts",
            ),
        )
    )
    facts.extend(
        _group_sum_facts(
            "cashflow",
            cashflow_rows,
            group_fields=("symbol", "currency"),
            sum_fields=("net_cashflow_gross", "premium_received_gross", "assignment_stock_net_cashflow_gross"),
        )
    )
    facts.extend(
        _group_sum_facts(
            "premium",
            premium_rows,
            group_fields=("symbol", "currency"),
            sum_fields=("premium_received_gross", "contracts"),
        )
    )
    facts.extend(
        _group_sum_facts(
            "realized",
            realized_rows,
            group_fields=("symbol", "currency"),
            sum_fields=("realized_gross", "contracts_closed"),
        )
    )
    facts.extend(
        _group_sum_facts(
            "assignment_lifecycle",
            assignment_rows,
            group_fields=("symbol", "currency"),
            sum_fields=(
                "assigned_contracts",
                "assignment_buy_cash_hkd",
                "premium_hkd",
                "realized_hkd",
                "assignment_lifecycle_pnl",
                "option_premium_attribution",
                "assigned_stock_unrealized_pnl",
                "assigned_stock_realized_pnl",
            ),
        )
    )
    facts.extend(
        _group_sum_facts(
            "enhancement",
            enhancement_rows,
            group_fields=("symbol", "currency"),
            sum_fields=("realized_gross", "contracts_closed", "open_amount_gross", "close_amount_gross"),
        )
    )
    facts.extend(
        _row_facts(
            "return_summary",
            return_summary,
            (
                "month",
                "account",
                "cash_secured_cny",
                "net_income_cny",
                "realized_pnl_cny",
                "premium_income_cny",
                "net_return_rate",
                "annualized_net_return_rate",
            ),
        )
    )
    facts.extend(
        _row_facts(
            "summary",
            summary_rows,
            (
                "month",
                "account",
                "currency",
                "net_cashflow_gross",
                "realized_pnl_gross",
                "premium_received_gross",
                "assignment_stock_net_cashflow_gross",
            ),
        )
    )
    facts.extend(
        _row_facts(
            "symbol_income",
            data.get("rows") or cashflow_rows,
            ("month", "account", "symbol", "currency", "component", "amount_gross", "source_view"),
        )
    )
    facts.extend(
        _row_facts(
            "assignment_lifecycle",
            assignment_rows,
            (
                "month",
                "account",
                "symbol",
                "currency",
                "quote_status",
                "stock_cost_per_share",
                "option_premium_attribution",
                "assignment_lifecycle_pnl",
            ),
        )
    )
    facts.extend(
        _row_facts(
            "enhancement",
            enhancement_rows,
            (
                "month",
                "account",
                "symbol",
                "currency",
                "option_type",
                "position_side",
                "strategy",
                "leg_role",
                "contracts_closed",
                "realized_gross",
                "close_type",
            ),
            limit=2,
        )
    )
    facts.extend(
        _row_facts(
            "diagnostic",
            data.get("diagnostics"),
            ("account", "month", "status", "matched_trade_events_count", "matched_lots_count", "missing_fields"),
            limit=3,
        )
    )
    return facts


def _monthly_income_evidence_context(data: dict[str, Any]) -> dict[str, str]:
    context = {
        "time_scope": "requested_month",
        "record_type": "monthly_income_and_trade_event_attribution",
        "use_as": "monthly option operation history evidence",
    }
    dimensions = []
    if _monthly_income_has_profit_quality_signal(data):
        dimensions.append("profit quality")
    if _monthly_income_has_assignment_cash_signal(data):
        dimensions.append("assignment cash outlay")
    if dimensions:
        context["answer_dimensions"] = ", ".join(dimensions)
    return context


def _monthly_income_has_profit_quality_signal(data: dict[str, Any]) -> bool:
    return any(
        _list_count(data, key) > 0
        for key in (
            "summary",
            "rows",
            "cashflow_rows",
            "premium_rows",
            "realized_rows",
            "enhancement_rows",
        )
    )


def _monthly_income_has_assignment_cash_signal(data: dict[str, Any]) -> bool:
    if _list_count(data, "assignment_lifecycle_rows") > 0:
        return True
    for key in ("summary", "rows", "cashflow_rows"):
        rows = data.get(key)
        if isinstance(rows, list) and any(_row_has_nonzero_assignment_cash(row) for row in rows):
            return True
    return False


def _row_has_nonzero_assignment_cash(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    for field_name in (
        "assignment_buy_cash_hkd",
        "assignment_stock_net_cashflow_gross",
        "assignment_stock_cashflow",
        "assigned_stock_cashflow",
    ):
        if _numeric_value(row.get(field_name)):
            return True
    return False


def _monthly_income_evidence_ok(data: dict[str, Any]) -> bool:
    for key in (
        "summary",
        "rows",
        "cashflow_rows",
        "premium_rows",
        "realized_rows",
        "assignment_lifecycle_rows",
        "enhancement_rows",
    ):
        if _list_count(data, key) > 0:
            return True
    return False


def _compact_option_positions(data: dict[str, Any]) -> str:
    row_count = int(data.get("row_count") or _list_count(data, "rows"))
    fragments = [f"open_position_rows={row_count}"]
    return "option_positions_read " + "; ".join(fragments) + "."


def _option_positions_facts(data: dict[str, Any]) -> list[str]:
    rows = data.get("rows")
    facts = [
        "position.scope: current_open_positions",
        "position.record_type: current_position_snapshot_not_monthly_transaction_history",
        f"open_position_rows={int(data.get('row_count') or _list_count(data, 'rows'))}",
    ]
    facts.extend(_numeric_total_facts("position", rows, ("contracts_open",)))
    facts.extend(
        _group_sum_facts(
            "position",
            rows,
            group_fields=("symbol", "currency"),
            sum_fields=("contracts_open", "cash_secured_amount"),
        )
    )
    facts.extend(
        _group_sum_facts(
            "position",
            rows,
            group_fields=("symbol", "currency", "option_type", "side"),
            sum_fields=("contracts_open", "cash_secured_amount"),
        )
    )
    facts.extend(
        _row_facts(
            "position",
            rows,
            (
                "account",
                "symbol",
                "option_type",
                "side",
                "strike",
                "expiration_ymd",
                "days_to_expiration",
                "contracts_open",
                "currency",
                "cash_secured_amount",
                "status",
            ),
        )
    )
    return facts


def _option_positions_evidence_ok(data: dict[str, Any]) -> bool:
    rows = data.get("rows")
    if isinstance(rows, list):
        return True
    row_count = _int_count(data.get("row_count"))
    if row_count > 0:
        return False
    return _has_explicit_row_count(data) and row_count == 0


def _option_positions_missing_evidence(_data: dict[str, Any], *, evidence_ok: bool) -> list[str]:
    return [] if evidence_ok else ["option_positions_read rows"]


def _compact_close_advice(data: dict[str, Any]) -> str:
    row_count = int(data.get("row_count") or _list_count(data, "rows"))
    fragments = [f"advice_rows={row_count}"]
    for key in ("matched_count", "returned_count"):
        if data.get(key) is not None:
            fragments.append(f"{key}={data.get(key)}")

    summary = data.get("summary")
    if isinstance(summary, dict):
        summary_fields = ", ".join(sorted(str(key) for key in summary)[:8])
        if summary_fields:
            fragments.append(f"summary_fields={summary_fields}")
    return "close_advice_read " + "; ".join(fragments) + "."


def _close_advice_facts(data: dict[str, Any]) -> list[str]:
    facts = [
        "close_advice.scope: latest_available_snapshot",
        "close_advice.record_type: exit_signal_not_monthly_transaction_history",
        f"advice_rows={int(data.get('row_count') or _list_count(data, 'rows'))}",
    ]
    summary = data.get("summary")
    summary_count_fields: set[str] = set()
    if isinstance(summary, dict):
        facts.extend(_count_map_facts("close_advice", summary, ("tier_counts", "action_counts", "evaluation_counts")))
        summary_count_fields = {
            field_name
            for field_name in ("tier_counts", "action_counts", "evaluation_counts")
            if isinstance(summary.get(field_name), dict)
        }
        facts.append(_mapping_fact("summary", summary, tuple(sorted(str(key) for key in summary)[:8])))
    missing_count_fields = []
    if "tier_counts" not in summary_count_fields:
        missing_count_fields.append(("tier", "tier_counts"))
    if "action_counts" not in summary_count_fields:
        missing_count_fields.append(("close_action", "action_counts"))
    if "evaluation_counts" not in summary_count_fields:
        missing_count_fields.append(("evaluation_status", "evaluation_counts"))
    facts.extend(_row_count_map_facts("close_advice", data.get("rows"), tuple(missing_count_fields)))
    facts.extend(
        _row_facts(
            "close_advice",
            data.get("rows"),
            (
                "symbol",
                "close_action",
                "tier",
                "reason",
                "account",
                "option_type",
                "side",
                "contracts_open",
                "expiration",
                "strike",
                "evaluation_status",
                "quote_status",
                "realized_if_close",
                "remaining_premium",
                "dte",
            ),
        )
    )
    return facts


def _close_advice_evidence_ok(data: dict[str, Any]) -> bool:
    if "returned_count" in data:
        if _int_count(data.get("returned_count")) > 0:
            return True
        return (
            _has_explicit_row_count(data)
            and _int_count(data.get("row_count")) == 0
            and _int_count(data.get("matched_count")) == 0
            and isinstance(data.get("rows"), list)
        )
    if "matched_count" in data:
        if _int_count(data.get("matched_count")) <= 0:
            return _has_explicit_row_count(data) and _int_count(data.get("row_count")) == 0 and isinstance(data.get("rows"), list)
        return (_int_count(data.get("row_count")) or _list_count(data, "rows")) > 0
    row_count = _int_count(data.get("row_count"))
    row_count_is_explicit = _has_explicit_row_count(data)
    rows = data.get("rows")
    if _list_count(data, "rows") > 0:
        return True
    if row_count > 0:
        return False
    return row_count_is_explicit and row_count == 0 and isinstance(rows, list)


def _close_advice_missing_evidence(_data: dict[str, Any], *, evidence_ok: bool) -> list[str]:
    return [] if evidence_ok else ["close_advice_read rows"]


def _row_count_map_facts(label: str, rows: Any, fields: tuple[tuple[str, str], ...]) -> list[str]:
    if not isinstance(rows, list) or not fields:
        return []
    facts: list[str] = []
    for row_field, fact_field in fields:
        counts: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = _compact_text(row.get(row_field), MAX_TOOL_FIELD_CHARS)
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
        parts = [f"{key}={count}" for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:6]]
        if parts:
            facts.append(f"{label}.{fact_field}: " + ", ".join(parts))
    return facts


def _numeric_total_facts(label: str, rows: Any, fields: tuple[str, ...]) -> list[str]:
    if not isinstance(rows, list):
        return []
    facts: list[str] = []
    for field_name in fields:
        total = _sum_numeric(rows, field_name)
        if total is not None:
            facts.append(f"{label}.{field_name}_total={_format_number(total)}")
    return facts


def _rows_have_any(rows: list[Any], fields: tuple[str, ...]) -> bool:
    return any(isinstance(row, dict) and any(field in row for field in fields) for row in rows)


def _rows_have_all(rows: list[Any], fields: tuple[str, ...]) -> bool:
    return any(isinstance(row, dict) and all(field in row for field in fields) for row in rows)


def _group_sum_facts(
    label: str,
    rows: Any,
    *,
    group_fields: tuple[str, ...],
    sum_fields: tuple[str, ...],
    limit: int = 4,
) -> list[str]:
    if not isinstance(rows, list):
        return []
    facts: list[str] = []
    for sum_field in sum_fields:
        grouped: dict[tuple[str, ...], float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = _numeric_value(row.get(sum_field))
            if value is None:
                continue
            group_key = tuple(_compact_text(row.get(field), MAX_TOOL_FIELD_CHARS) for field in group_fields)
            if not any(group_key):
                continue
            grouped[group_key] = grouped.get(group_key, 0.0) + value
        if not grouped:
            continue
        parts = []
        for group_key, value in sorted(grouped.items(), key=lambda item: (-abs(item[1]), item[0]))[: max(0, limit)]:
            key_text = "/".join(part or "unknown" for part in group_key)
            parts.append(f"{key_text}={_format_number(value)}")
        if parts:
            facts.append(f"{label}.{sum_field}_by_{'_'.join(group_fields)}: " + ", ".join(parts))
    return facts


def _count_map_facts(label: str, data: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    facts: list[str] = []
    for field_name in fields:
        value = data.get(field_name)
        if not isinstance(value, dict):
            continue
        parts = []
        for key, count in sorted(value.items(), key=lambda item: (-(_numeric_value(item[1]) or 0), str(item[0])))[:6]:
            numeric = _numeric_value(count)
            if numeric is None:
                continue
            parts.append(f"{_compact_text(key, MAX_TOOL_FIELD_CHARS)}={_format_number(numeric)}")
        if parts:
            facts.append(f"{label}.{field_name}: " + ", ".join(parts))
    return facts


def _sum_numeric(rows: list[Any], field_name: str) -> float | None:
    total = 0.0
    found = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _numeric_value(row.get(field_name))
        if value is None:
            continue
        total += value
        found = True
    return total if found else None


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _row_facts(
    label: str,
    rows: Any,
    fields: tuple[str, ...],
    *,
    limit: int = MAX_ROW_FACTS_PER_GROUP,
) -> list[str]:
    if not isinstance(rows, list):
        return []
    facts: list[str] = []
    for index, row in enumerate(rows[: max(0, limit)], start=1):
        if not isinstance(row, dict):
            continue
        text = _mapping_fact(f"{label}[{index}]", row, fields)
        if text:
            facts.append(text)
    if len(rows) > limit:
        facts.append(f"{label}.remaining_rows={len(rows) - limit}")
    return facts


def _mapping_fact(label: str, row: dict[str, Any], fields: tuple[str, ...]) -> str:
    pairs: list[str] = []
    for field_name in fields:
        if field_name not in row:
            continue
        value = _fact_value(row.get(field_name))
        if value == "":
            continue
        pairs.append(f"{field_name}={value}")
        if len(pairs) >= MAX_ROW_FACT_FIELDS:
            break
    return f"{label}: " + ", ".join(pairs) if pairs else ""


def _fact_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return _compact_text(value, MAX_TOOL_FIELD_CHARS)
    if isinstance(value, list):
        return "[" + ", ".join(_fact_list_value(item) for item in value[:4]) + (", ..." if len(value) > 4 else "") + "]"
    if isinstance(value, dict):
        return _fact_nested_value(value)
    return _compact_text(type(value).__name__, MAX_TOOL_FIELD_CHARS)


def _fact_list_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return _fact_nested_value(value)
    return _compact_text(value, 40)


def _fact_nested_value(value: Any) -> str:
    if isinstance(value, dict):
        keys = sorted(str(key) for key in value)[:5]
        return "dict keys=" + ",".join(keys)
    if isinstance(value, (list, tuple, set)):
        return f"{type(value).__name__} count={len(value)}"
    return type(value).__name__


def _analysis_coverage(data: dict[str, Any]) -> str:
    evidence = data.get("evidence")
    if not isinstance(evidence, dict):
        return ""
    coverage = evidence.get("coverage")
    if not isinstance(coverage, dict):
        return ""
    fragments: list[str] = []
    for key in ("row_count", "month_count", "account_count", "symbol_count"):
        if coverage.get(key) is not None:
            fragments.append(f"{key}:{coverage.get(key)}")
    return ", ".join(fragments)


def _analysis_diagnostics(data: dict[str, Any]) -> str:
    evidence = data.get("evidence")
    if not isinstance(evidence, dict):
        return ""
    diagnostics = evidence.get("diagnostics")
    if isinstance(diagnostics, list):
        warning_count = sum(
            1
            for item in diagnostics
            if isinstance(item, dict) and str(item.get("severity") or "").strip().lower() == "warning"
        )
        fragments = [f"diagnostic_count:{len(diagnostics)}"]
        if warning_count:
            fragments.append(f"warning_count:{warning_count}")
        return ", ".join(fragments)
    if not isinstance(diagnostics, dict):
        return ""
    fragments: list[str] = []
    for key in ("warning_count", "missing_view_count", "stale_view_count"):
        if diagnostics.get(key) is not None:
            fragments.append(f"{key}:{diagnostics.get(key)}")
    return ", ".join(fragments)


def _analysis_query_evidence_ok(data: dict[str, Any]) -> bool:
    if _analysis_row_count(data) <= 0 and not _has_valid_empty_analysis_view(data):
        return False
    if _empty_filtered_view_items(data):
        return False
    return not _has_problem_diagnostics(data.get("evidence")) and not _has_problem_diagnostics(data.get("query_explain"))


def _has_problem_diagnostics(value: Any) -> bool:
    diagnostics = value.get("diagnostics") if isinstance(value, dict) else None
    if isinstance(diagnostics, list):
        return any(
            isinstance(item, dict) and str(item.get("severity") or "").strip().lower() == "warning"
            for item in diagnostics
        )
    if isinstance(diagnostics, dict):
        return any(_int_count(diagnostics.get(key)) > 0 for key in _PROBLEM_DIAGNOSTIC_COUNT_KEYS)
    return False


def _list_count(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    return len(value) if isinstance(value, list) else 0


def _analysis_row_count(data: dict[str, Any]) -> int:
    count = _int_count(data.get("row_count"))
    if count > 0:
        return count
    rows_count = _list_count(data, "rows")
    if rows_count > 0:
        return rows_count
    view_datasets = data.get("view_datasets")
    if not isinstance(view_datasets, dict):
        return 0
    total = 0
    for dataset in view_datasets.values():
        if not isinstance(dataset, dict):
            continue
        total += _int_count(dataset.get("row_count")) or _list_count(dataset, "rows")
    return total


def _diagnostic_missing_items(label: str, value: Any) -> list[str]:
    diagnostics = value.get("diagnostics") if isinstance(value, dict) else None
    if isinstance(diagnostics, dict):
        return [
            f"{label} diagnostic: {key}={_int_count(diagnostics.get(key))}"
            for key in _PROBLEM_DIAGNOSTIC_COUNT_KEYS
            if _int_count(diagnostics.get(key)) > 0
        ]
    if not isinstance(diagnostics, list):
        return []
    missing: list[str] = []
    for item in diagnostics[:3]:
        if not isinstance(item, dict):
            continue
        view = _compact_text(item.get("view"), 60)
        status = _compact_text(item.get("status"), 60)
        severity = _compact_text(item.get("severity"), 40)
        parts = [part for part in (view, status, severity) if part]
        if parts:
            missing.append(f"{label} diagnostic: " + "/".join(parts))
    return missing


def _empty_filtered_view_items(data: dict[str, Any]) -> list[str]:
    query = data.get("query")
    if not isinstance(query, dict) or not isinstance(query.get("filters"), dict) or not query.get("filters"):
        return []
    view_datasets = data.get("view_datasets")
    missing: list[str] = []
    warning_views = _diagnostic_warning_views(data)
    view_names: list[str] = []
    views_used = data.get("views_used")
    if isinstance(views_used, list):
        for item in views_used:
            view_name = str(item)
            if view_name and view_name not in view_names:
                view_names.append(view_name)
            if len(view_names) >= 8:
                break
    if not isinstance(view_datasets, dict):
        if _analysis_row_count(data) <= 0:
            return [f"analysis_query filtered view empty: {view_name}" for view_name in view_names if view_name not in warning_views]
        return []
    for view_name in _ordered_view_dataset_names(data):
        if len(view_names) >= 8:
            break
        if view_name not in view_names:
            view_names.append(view_name)
    for view_name in view_names:
        if view_name in warning_views:
            continue
        dataset = view_datasets.get(view_name)
        if not isinstance(dataset, dict):
            missing.append(f"analysis_query filtered view empty: {view_name}")
            continue
        if _int_count(dataset.get("row_count")) > 0 and not isinstance(dataset.get("rows"), list):
            missing.append(f"analysis_query filtered view rows unavailable: {view_name}")
            continue
        if (_int_count(dataset.get("row_count")) or _list_count(dataset, "rows")) == 0:
            if _empty_view_dataset_is_valid_negative_evidence(view_name, dataset):
                continue
            missing.append(f"analysis_query filtered view empty: {view_name}")
    return missing


def _has_valid_empty_analysis_view(data: dict[str, Any]) -> bool:
    view_datasets = data.get("view_datasets")
    if not isinstance(view_datasets, dict):
        return False
    has_valid_empty = False
    for view_name, dataset in view_datasets.items():
        if not isinstance(dataset, dict):
            continue
        if (_int_count(dataset.get("row_count")) or _list_count(dataset, "rows")) > 0:
            continue
        if _empty_view_dataset_is_valid_negative_evidence(str(view_name), dataset):
            has_valid_empty = True
            continue
        if _has_explicit_row_count(dataset) or isinstance(dataset.get("rows"), list):
            return False
    return has_valid_empty


def _empty_view_dataset_is_valid_negative_evidence(view_name: str, dataset: dict[str, Any]) -> bool:
    allowed_meanings = VALID_EMPTY_RESULT_MEANINGS_BY_VIEW.get(view_name)
    if not allowed_meanings:
        return False
    if str(dataset.get("empty_result_meaning") or "").strip() not in allowed_meanings:
        return False
    if (_int_count(dataset.get("row_count")) or _list_count(dataset, "rows")) > 0:
        return False
    return _has_explicit_row_count(dataset) or isinstance(dataset.get("rows"), list)


def _diagnostic_warning_views(data: dict[str, Any]) -> set[str]:
    views: set[str] = set()
    for source in (data.get("evidence"), data.get("query_explain")):
        diagnostics = source.get("diagnostics") if isinstance(source, dict) else None
        if not isinstance(diagnostics, list):
            continue
        for item in diagnostics:
            if not isinstance(item, dict):
                continue
            if str(item.get("severity") or "").strip().lower() != "warning":
                continue
            view = str(item.get("view") or "").strip()
            if view:
                views.add(view)
    return views


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _int_count(value: Any) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return 0


def _has_explicit_row_count(data: dict[str, Any]) -> bool:
    if "row_count" not in data:
        return False
    value = data.get("row_count")
    if isinstance(value, bool):
        return False
    try:
        int(value)
    except Exception:
        return False
    return True


def _warning_text(warnings: Any) -> str:
    if not warnings:
        return ""
    if isinstance(warnings, list):
        return f"warning_count={len(warnings)}"
    return "warning_count=1"


def _value_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    preview: dict[str, Any] = {}
    keys = sorted(str(key) for key in value)[:12]
    for key in keys:
        preview[key] = _preview_value(value.get(key))
    if len(value) > len(keys):
        preview["_truncated_keys"] = len(value) - len(keys)
    return preview


def _preview_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 120 else f"{value[:117]}..."
    if isinstance(value, list):
        return {"type": "list", "count": len(value)}
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(str(key) for key in value)[:8]}
    return {"type": type(value).__name__}


TOOL_VIEWS: dict[str, AgentToolView] = {
    "runtime_status": AgentToolView(
        name="runtime_status",
        required_scene_fields=("config_key",),
        payload_fields={"config_key": "config_key"},
        evidence_context={
            "time_scope": "current_runtime_snapshot",
            "record_type": "runtime_health_status",
            "use_as": "operational readiness evidence",
        },
        observation_summary=_compact_runtime_status,
        observation_facts=_runtime_status_facts,
    ),
    "candidate_filter_explain": AgentToolView(
        name="candidate_filter_explain",
        required_scene_fields=("symbol",),
        payload_fields={"symbol": "symbol", "config_key": "config_key"},
        evidence_context={
            "time_scope": "latest_candidate_trace_snapshot",
            "record_type": "candidate_filter_trace",
            "use_as": "filter decision evidence",
        },
        observation_summary=_compact_candidate_filter,
        observation_facts=_candidate_filter_facts,
        evidence_available=lambda data: int(data.get("trace_count") or 0) > 0,
        missing_evidence=lambda _data, *, evidence_ok: [] if evidence_ok else ["candidate filter trace rows"],
    ),
    "analysis_catalog": AgentToolView(
        name="analysis_catalog",
        required_scene_fields=("config_key",),
        payload_fields={"config_key": "config_key"},
        claimable=False,
        evidence_context={
            "time_scope": "analysis_workspace_metadata",
            "record_type": "view_catalog",
            "use_as": "tool setup metadata, not business claim evidence",
        },
        observation_summary=_compact_analysis_catalog,
        observation_facts=_analysis_catalog_facts,
        evidence_available=lambda data: int(data.get("view_count") or 0) > 0,
        missing_evidence=lambda _data, *, evidence_ok: [] if evidence_ok else ["analysis_catalog views"],
    ),
    "analysis_query": AgentToolView(
        name="analysis_query",
        required_scene_fields=("config_key",),
        payload_fields={"config_key": "config_key", "month": "month"},
        evidence_context={
            "time_scope": "requested_filters",
            "record_type": "approved_analysis_view_rows",
            "use_as": "materialized analysis cross-check evidence",
        },
        observation_evidence_context=_analysis_query_evidence_context,
        observation_summary=_compact_analysis_query,
        observation_facts=_analysis_query_facts,
        evidence_available=_analysis_query_evidence_ok,
        missing_evidence=_analysis_query_missing_evidence,
    ),
    "monthly_income_report": AgentToolView(
        name="monthly_income_report",
        required_scene_fields=("config_key", "month"),
        payload_fields={"config_key": "config_key", "month": "month"},
        evidence_context={
            "time_scope": "requested_month",
            "record_type": "monthly_income_and_trade_event_attribution",
            "use_as": "monthly option operation history evidence",
            "answer_dimensions": "profit quality, assignment cash outlay",
        },
        observation_evidence_context=_monthly_income_evidence_context,
        observation_summary=_compact_monthly_income,
        observation_facts=_monthly_income_facts,
        evidence_available=_monthly_income_evidence_ok,
        missing_evidence=_monthly_income_missing_evidence,
    ),
    "option_positions_read": AgentToolView(
        name="option_positions_read",
        required_scene_fields=("config_key",),
        payload_fields={"config_key": "config_key"},
        evidence_context={
            "time_scope": "current_snapshot",
            "record_type": "current_open_position_snapshot",
            "use_as": "current exposure evidence",
            "not_evidence_for": "monthly transaction history or closed-trade history",
            "answer_dimensions": "open-exposure concentration",
        },
        observation_summary=_compact_option_positions,
        observation_facts=_option_positions_facts,
        evidence_available=_option_positions_evidence_ok,
        missing_evidence=_option_positions_missing_evidence,
    ),
    "close_advice_read": AgentToolView(
        name="close_advice_read",
        required_scene_fields=("config_key",),
        payload_fields={"config_key": "config_key"},
        evidence_context={
            "time_scope": "latest_available_snapshot",
            "record_type": "exit_signal_snapshot",
            "use_as": "current close-advice signal evidence",
            "not_evidence_for": "monthly transaction history or realized trade history",
            "answer_dimensions": "current close-advice signals",
        },
        observation_summary=_compact_close_advice,
        observation_facts=_close_advice_facts,
        evidence_available=_close_advice_evidence_ok,
        missing_evidence=_close_advice_missing_evidence,
    ),
}
