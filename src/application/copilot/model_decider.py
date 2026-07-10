from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from src.application.copilot.agent import AgentAction, AgentState, default_action_decider
from src.application.copilot.safety_text import contains_forbidden_external_action_claim


StructuredActionModel = Callable[[dict[str, Any]], dict[str, Any]]
MAX_MODEL_SUMMARY_CHARS = 600
MAX_MODEL_MISSING_DATA_CHARS = 160
MAX_MODEL_GUIDANCE_CHARS = 400
MAX_MODEL_FACT_CHARS = 260
MAX_MODEL_FACTS = 28


@dataclass
class ModelActionDecider:
    model: StructuredActionModel
    max_repairs: int = 1
    disable_after_model_error: bool = True
    _model_disabled: bool = field(default=False, init=False, repr=False)

    def __call__(self, state: AgentState) -> AgentAction:
        if _unattempted_tools_without_evidence(state):
            return default_action_decider(state)
        if self._model_disabled:
            return default_action_decider(state)

        request = _action_request(state)
        last_error = ""
        for attempt in range(max(0, self.max_repairs) + 1):
            try:
                raw = self.model(request)
            except Exception as exc:
                if self.disable_after_model_error:
                    self._model_disabled = True
                fallback = default_action_decider(state)
                return AgentAction(
                    kind=fallback.kind,
                    tool_name=fallback.tool_name,
                    reason=f"model action error: {exc.__class__.__name__}; deterministic tool collection",
                    final_report=fallback.final_report,
                    error_code="MODEL_ERROR",
                )

            action, error = _parse_model_action(
                raw,
                allowed_tools=state.manifest.allowed_tools,
                claimable_refs=_claimable_refs(state.observations),
                unattempted_tools_without_evidence=_unattempted_tools_without_evidence(state),
                missing_allowed_tool_evidence=_missing_allowed_tool_evidence(
                    state.observations,
                    state.manifest.allowed_tools,
                ),
                requested_scope_refs=_claimable_refs_by_context(state.observations, _requested_scope_context),
                attempted_tools=state.attempted_tools,
                execution_environment=state.manifest.execution_environment,
                requires_recommendations=_requires_recommendations(state),
                answer_dimensions=_answer_dimensions(state),
            )
            if action:
                return action

            last_error = error
            request = _action_request(
                state,
                repair_error=error,
                previous_response=raw,
                repair_attempt=attempt + 1,
            )

        return AgentAction(
            kind="invalid",
            reason=f"model action invalid: {last_error or 'unknown error'}",
            error_code="MODEL_ACTION_INVALID",
        )


def _action_request(
    state: AgentState,
    *,
    repair_error: str | None = None,
    previous_response: dict[str, Any] | None = None,
    repair_attempt: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "user_message": _user_message_from_manifest(state.manifest.messages),
        "execution_environment": state.manifest.execution_environment,
        "task_guidance": _model_task_guidance(state.manifest.task_guidance),
        "allowed_tools": list(state.manifest.allowed_tools),
        "tools": _model_tool_descriptions(state.manifest.tool_descriptions, state.manifest.allowed_tools),
        "attempted_tools": list(state.attempted_tools),
        "observations": [_model_observation(item) for item in state.observations],
        "remaining_budget": _remaining_budget(state),
        "finish_conditions": _finish_conditions(state),
        "quality_contract": _quality_contract(state),
        "response_schema": {
            "kind": "tool|finish",
            "tool_name": "required when kind=tool; null when kind=finish",
            "reason": "short reason for the action",
            "answer_report": (
                "null when kind=tool; object when kind=finish. "
                "A finish report must start conclusion with '结论', include cited findings, "
                "cite only claimable observation refs, state missing_data when evidence is incomplete, "
                "and never claim writes, notifications sent, orders, config changes, deployments, or service changes."
            ),
        },
    }
    if repair_error:
        payload["repair"] = {
            "attempt": repair_attempt,
            "error": repair_error,
            "previous_response": _repair_previous_response(previous_response),
        }
    return payload


def _quality_contract(state: AgentState) -> dict[str, Any]:
    guidance = state.manifest.task_guidance if isinstance(state.manifest.task_guidance, dict) else {}
    contract: dict[str, Any] = {
        "answer_style": [
            "Answer the user's question directly first.",
            "Use observations as evidence, not as rows to dump.",
            "Make evidence gaps explicit instead of inventing missing facts.",
            "Cite observation refs in findings and recommendation basis_refs.",
        ],
        "safety": [
            "The run is read-only.",
            "Do not claim external writes, notifications sent, broker actions, config changes, deployments, or service changes.",
        ],
    }
    if guidance.get("requires_recommendations") is True:
        contract["recommendations"] = [
            "Include concrete next steps only when supported by cited findings.",
            "Leave recommendations empty when material evidence is missing.",
        ]
    dimensions = _model_string_list(guidance.get("answer_dimensions"))
    if dimensions:
        contract["answer_dimensions"] = dimensions
    return contract


def _model_summary(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return f"{type(value).__name__} summary"
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_MODEL_SUMMARY_CHARS:
        return text
    return f"{text[: MAX_MODEL_SUMMARY_CHARS - 3]}..."


def _model_tool_descriptions(value: Any, allowed_tools: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    allowed = set(allowed_tools)
    tools: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name not in allowed:
            continue
        tools.append(
            {
                "name": name,
                "description": _model_summary(item.get("description")),
                "capabilities": _model_string_list(item.get("capabilities")),
                "evidence_context": _model_string_map(item.get("evidence_context")),
                "input_fields": _model_string_list(item.get("input_fields")),
                "output_contract": _model_output_contract(item.get("output_contract")),
            }
        )
    return tools


def _model_observation(item: dict[str, Any]) -> dict[str, Any]:
    facts, omitted_fact_count = _model_facts(item.get("facts"))
    observation = {
        "ref": _model_ref(item.get("ref")),
        "tool_name": item.get("tool_name"),
        "ok": item.get("ok"),
        "evidence_ok": item.get("evidence_ok"),
        "claimable": _observation_claimable(item),
        "evidence_context": _model_string_map(item.get("evidence_context")),
        "summary": _model_summary(item.get("summary")),
        "facts": facts,
        "missing_data": _model_missing_data(item.get("missing_data")),
    }
    facts_omitted = min(1_000_000, _bounded_count(item.get("facts_omitted")) + omitted_fact_count)
    if facts_omitted:
        observation["facts_omitted"] = facts_omitted
    return observation


def _model_output_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, Any] = {}
    for key, item in value.items():
        key_text = " ".join(str(key or "").split())[:120]
        if not key_text:
            continue
        if isinstance(item, list):
            output[key_text] = _model_string_list(item)
        elif isinstance(item, dict):
            output[key_text] = "dict value"
        elif isinstance(item, bool):
            output[key_text] = item
        elif isinstance(item, (int, float)):
            output[key_text] = item
        else:
            text = " ".join(str(item or "").split())[:120]
            if text:
                output[key_text] = text
    return output


def _model_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        if isinstance(item, (dict, list, tuple, set)):
            text = f"{type(item).__name__} value"
        else:
            text = " ".join(str(item or "").split())
        if text:
            items.append(text[:120])
    return items


def _model_string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        key_text = " ".join(str(key or "").split())[:120]
        if isinstance(item, (dict, list, tuple, set)):
            value_text = f"{type(item).__name__} value"
        else:
            value_text = " ".join(str(item or "").split())[:120]
        if key_text and value_text:
            result[key_text] = value_text
    return result


def _user_message_from_manifest(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _model_task_guidance(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"instructions": [], "requires_answer_synthesis": False}
    raw_instructions = value.get("instructions")
    instructions: list[str] = []
    if isinstance(raw_instructions, list):
        for item in raw_instructions:
            text = _guidance_item(item)
            if text:
                instructions.append(text)
    return {
        "instructions": instructions,
        "answer_dimensions": _model_string_list(value.get("answer_dimensions")),
        "requires_answer_synthesis": bool(value.get("requires_answer_synthesis") is True),
        "requires_recommendations": bool(value.get("requires_recommendations") is True),
    }


def _guidance_item(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return f"{type(value).__name__} guidance"
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_MODEL_GUIDANCE_CHARS:
        return text
    return f"{text[: MAX_MODEL_GUIDANCE_CHARS - 3]}..."


def _remaining_budget(state: AgentState) -> dict[str, int]:
    max_turns = int(state.manifest.limits.get("max_model_turns") or 0)
    max_tool_calls = int(state.manifest.limits.get("max_tool_calls") or 0)
    return {
        "turns": max(0, max_turns - state.turns),
        "tool_calls": max(0, max_tool_calls - state.tool_calls),
    }


def _finish_conditions(state: AgentState) -> dict[str, Any]:
    conditions = {
        "requires_visible_evidence_refs": True,
        "requires_cited_findings": True,
        "requires_recommendations": _requires_recommendations(state),
        "claimable_refs": _claimable_refs(state.observations),
        "claimable_refs_by_tool": _claimable_refs_by_tool(state.observations),
        "claimable_ref_context": _claimable_ref_context(state.observations),
        "unattempted_tools_without_evidence": _unattempted_tools_without_evidence(state),
        "attempted_tools_without_evidence": _attempted_tools_without_evidence(state),
        "missing_allowed_tool_evidence": _missing_allowed_tool_evidence(
            state.observations,
            state.manifest.allowed_tools,
        ),
    }
    requested_scope_refs = _claimable_refs_by_context(state.observations, _requested_scope_context)
    current_context_refs = _claimable_refs_by_context(state.observations, _current_context)
    if requested_scope_refs:
        conditions["requested_scope_refs"] = requested_scope_refs
    if current_context_refs:
        conditions["current_context_refs"] = current_context_refs
    return conditions


def _requires_recommendations(state: AgentState) -> bool:
    guidance = state.manifest.task_guidance
    return isinstance(guidance, dict) and guidance.get("requires_recommendations") is True


def _answer_dimensions(state: AgentState) -> list[str]:
    guidance = state.manifest.task_guidance
    if not isinstance(guidance, dict):
        return []
    return _model_string_list(guidance.get("answer_dimensions"))


def _evidence_ok_tools(observations: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("tool_name"))
        for item in observations
        if bool(item.get("ok")) and bool(item.get("evidence_ok", item.get("ok")))
    }


def _claimable_refs(observations: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("ref"))
        for item in observations
        if isinstance(item.get("ref"), str) and _observation_claimable(item)
    ]


def _claimable_refs_by_tool(observations: list[dict[str, Any]]) -> dict[str, list[str]]:
    refs_by_tool: dict[str, list[str]] = {}
    for item in observations:
        if not _observation_claimable(item):
            continue
        ref = _model_ref(item.get("ref"))
        tool_name = str(item.get("tool_name") or "").strip()
        if ref and tool_name:
            refs_by_tool.setdefault(tool_name, []).append(ref)
    return refs_by_tool


def _claimable_ref_context(observations: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    refs: dict[str, dict[str, str]] = {}
    for item in observations:
        if not _observation_claimable(item):
            continue
        ref = _model_ref(item.get("ref"))
        tool_name = str(item.get("tool_name") or "").strip()
        if not ref:
            continue
        context = _model_string_map(item.get("evidence_context"))
        if tool_name:
            context = {"tool_name": tool_name, **context}
        refs[ref] = context
    return refs


def _claimable_refs_by_context(observations: list[dict[str, Any]], predicate: Callable[[dict[str, str]], bool]) -> list[str]:
    refs: list[str] = []
    for item in observations:
        if not _observation_claimable(item):
            continue
        ref = _model_ref(item.get("ref"))
        context = _model_string_map(item.get("evidence_context"))
        if ref and predicate(context):
            refs.append(ref)
    return refs


def _requested_scope_context(context: dict[str, str]) -> bool:
    return str(context.get("time_scope") or "").startswith("requested")


def _current_context(context: dict[str, str]) -> bool:
    time_scope = str(context.get("time_scope") or "")
    return time_scope.startswith("current") or time_scope.startswith("latest")


def _observation_claimable(item: dict[str, Any]) -> bool:
    return bool(item.get("claimable", True)) and bool(item.get("ok")) and bool(item.get("evidence_ok", item.get("ok")))


def _missing_allowed_tool_evidence(observations: list[dict[str, Any]], allowed_tools: list[str]) -> list[dict[str, Any]]:
    allowed = set(allowed_tools)
    missing: list[dict[str, Any]] = []
    for item in observations:
        tool_name = str(item.get("tool_name") or "").strip()
        if tool_name not in allowed:
            continue
        if bool(item.get("ok")) and bool(item.get("evidence_ok", item.get("ok"))):
            continue
        missing_data = _model_missing_data(item.get("missing_data")) or [f"{tool_name} evidence unavailable"]
        missing.append({"tool_name": tool_name, "missing_data": missing_data})
    return missing


def _unattempted_tools_without_evidence(state: AgentState) -> list[str]:
    evidence_ok = _evidence_ok_tools(state.observations)
    attempted = {str(item or "") for item in state.attempted_tools}
    attempted.update(str(item.get("tool_name") or "") for item in state.observations)
    return [tool_name for tool_name in state.manifest.allowed_tools if tool_name not in evidence_ok and tool_name not in attempted]


def _attempted_tools_without_evidence(state: AgentState) -> list[str]:
    evidence_ok = _evidence_ok_tools(state.observations)
    attempted = {str(item or "") for item in state.attempted_tools}
    attempted.update(str(item.get("tool_name") or "") for item in state.observations)
    return [tool_name for tool_name in state.manifest.allowed_tools if tool_name not in evidence_ok and tool_name in attempted]


def _model_missing_data(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    preview: list[str] = []
    for item in value:
        text = _missing_data_item(item)
        if text:
            preview.append(text)
    return preview


def _bounded_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return min(max(0, int(value)), 1_000_000)
    except Exception:
        return 0


def _model_facts(value: Any) -> tuple[list[str], int]:
    if not isinstance(value, list):
        return [], 0
    facts: list[str] = []
    omitted = 0
    for item in value:
        if isinstance(item, (dict, list, tuple, set)):
            text = f"{type(item).__name__} fact"
        else:
            text = " ".join(str(item or "").split())
        if not text:
            continue
        if _model_fact_is_row_sample(text):
            omitted += 1
            continue
        if len(text) > MAX_MODEL_FACT_CHARS:
            text = f"{text[: MAX_MODEL_FACT_CHARS - 3]}..."
        facts.append(text)
        if len(facts) >= MAX_MODEL_FACTS:
            break
    return facts, omitted


def _model_fact_is_row_sample(text: str) -> bool:
    label = text.split(":", 1)[0].split("=", 1)[0]
    if label.startswith(("diagnostic[", "freshness[")):
        return False
    return "[" in label or label.endswith(".remaining_rows")


def _model_ref(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.startswith("obs_") else ""


def _missing_data_item(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return f"{type(value).__name__} missing value"
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_MODEL_MISSING_DATA_CHARS:
        return text
    return f"{text[: MAX_MODEL_MISSING_DATA_CHARS - 3]}..."


def _repair_previous_response(previous_response: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(previous_response, dict):
        return {}
    tool_name = str(previous_response.get("tool_name") or "").strip()
    return {
        "kind": previous_response.get("kind"),
        "has_tool_name": bool(tool_name),
        "has_answer_report": isinstance(previous_response.get("answer_report"), dict),
    }


def _parse_model_action(
    raw: dict[str, Any],
    *,
    allowed_tools: list[str],
    claimable_refs: list[str] | None = None,
    unattempted_tools_without_evidence: list[str] | None = None,
    missing_allowed_tool_evidence: list[dict[str, Any]] | None = None,
    requested_scope_refs: list[str] | None = None,
    attempted_tools: list[str] | None = None,
    execution_environment: str = "",
    requires_recommendations: bool = False,
    answer_dimensions: list[str] | None = None,
) -> tuple[AgentAction | None, str]:
    if not isinstance(raw, dict):
        return None, "model response must be an object"
    kind = str(raw.get("kind") or "").strip().lower()
    reason = str(raw.get("reason") or "").strip()
    if kind == "finish":
        if raw.get("tool_name") is not None:
            return None, "finish action requires null tool_name"
        final_report = raw.get("answer_report")
        if not isinstance(final_report, dict):
            return None, "finish action requires answer_report"
        if not _has_conclusion(final_report):
            return None, "finish action requires conclusion"
        if execution_environment == "eval" and not _has_eval_fixture_disclosure(final_report):
            return None, "finish action requires eval fixture disclosure"
        if contains_forbidden_external_action_claim(final_report):
            return None, "finish action claims external action"
        if unattempted_tools_without_evidence:
            return None, "finish action requires tool evidence"
        if claimable_refs and not _has_findings(final_report, claimable_refs=claimable_refs):
            return None, "finish action requires cited findings"
        if claimable_refs is not None and not _all_report_refs_are_claimable(final_report, claimable_refs):
            return None, "finish action uses non-claimable evidence refs"
        if _has_unsynthesized_detail_listing(final_report):
            return None, "finish action requires synthesized summaries"
        if missing_allowed_tool_evidence and not _reports_missing_tool_evidence(final_report, missing_allowed_tool_evidence):
            return None, "finish action requires missing evidence"
        if missing_allowed_tool_evidence and _has_recommendation_entries(final_report):
            return None, "finish action requires recommendations empty when evidence missing"
        if requires_recommendations and not missing_allowed_tool_evidence and answer_dimensions:
            if not _recommendations_have_allowed_dimensions(final_report, answer_dimensions):
                return None, "finish action requires recommendation answer dimension"
        if requires_recommendations and not missing_allowed_tool_evidence and not _has_recommendations(
            final_report,
            claimable_refs=claimable_refs,
            answer_dimensions=answer_dimensions,
        ):
            return None, "finish action requires recommendations"
        if requires_recommendations and not missing_allowed_tool_evidence:
            if not _recommendations_are_supported_by_findings(final_report, answer_dimensions=answer_dimensions):
                return None, "finish action requires recommendation finding support"
            if requested_scope_refs and not _recommendations_cite_any(final_report, requested_scope_refs):
                return None, "finish action requires requested-period recommendation evidence"
        return AgentAction(
            kind="finish",
            reason=reason or "model requested finish",
            final_report=final_report,
        ), ""

    if kind == "tool":
        tool_name = str(raw.get("tool_name") or "").strip()
        if not tool_name:
            return None, "tool action requires tool_name"
        if raw.get("answer_report") is not None:
            return None, "tool action requires null answer_report"
        if tool_name not in set(allowed_tools):
            return None, "tool action uses disallowed tool_name"
        if tool_name in {str(item or "") for item in attempted_tools or []}:
            return None, "tool action repeats attempted tool"
        return AgentAction(kind="tool", tool_name=tool_name, reason=reason or "model requested tool"), ""

    return None, f"unsupported action kind: {kind or '<missing>'}"


def _has_conclusion(final_report: dict[str, Any]) -> bool:
    conclusion = str(final_report.get("conclusion") or "").strip()
    return conclusion.startswith("结论") and len(conclusion) > len("结论")


def _has_eval_fixture_disclosure(final_report: dict[str, Any]) -> bool:
    conclusion = str(final_report.get("conclusion") or "")
    missing_data = _model_missing_data(final_report.get("missing_data"))
    return "eval-only" in conclusion and "fixture observations are not production evidence" in missing_data


def _has_findings(final_report: dict[str, Any], *, claimable_refs: list[str] | None = None) -> bool:
    findings = final_report.get("findings")
    if not isinstance(findings, list) or not findings:
        return False
    allowed_refs = {ref for ref in claimable_refs or [] if ref}
    if not allowed_refs:
        return True
    for item in findings:
        if not isinstance(item, dict):
            continue
        if not _non_empty_string(item.get("summary")):
            continue
        if any(ref in allowed_refs for ref in _model_string_list(item.get("evidence_refs"))):
            return True
    return False


def _has_recommendations(
    final_report: dict[str, Any],
    *,
    claimable_refs: list[str] | None = None,
    answer_dimensions: list[str] | None = None,
) -> bool:
    recommendations = final_report.get("recommendations")
    if not isinstance(recommendations, list) or not recommendations:
        return False
    allowed_refs = {ref for ref in claimable_refs or [] if ref}
    if claimable_refs is not None and not allowed_refs:
        return False
    for item in recommendations:
        if not isinstance(item, dict):
            continue
        if not _valid_recommendation_shape(item, answer_dimensions=answer_dimensions):
            continue
        if claimable_refs is None or any(ref in allowed_refs for ref in _model_string_list(item.get("basis_refs"))):
            return True
    return False


def _recommendations_have_allowed_dimensions(final_report: dict[str, Any], answer_dimensions: list[str]) -> bool:
    recommendations = final_report.get("recommendations")
    if not isinstance(recommendations, list) or not recommendations:
        return True
    return all(
        isinstance(item, dict) and _recommendation_dimension_allowed(item, answer_dimensions)
        for item in recommendations
    )


def _has_recommendation_entries(final_report: dict[str, Any]) -> bool:
    recommendations = final_report.get("recommendations")
    if not isinstance(recommendations, list):
        return bool(" ".join(str(recommendations or "").split()))
    for item in recommendations:
        if isinstance(item, dict):
            if any(_non_empty_string(item.get(field)) for field in ("summary", "action", "target_scope", "answer_dimension")):
                return True
            if _model_string_list(item.get("basis_refs")):
                return True
        elif " ".join(str(item or "").split()):
            return True
    return False


def _valid_recommendation_shape(item: dict[str, Any], *, answer_dimensions: list[str] | None = None) -> bool:
    return (
        _non_empty_string(item.get("summary"))
        and _non_empty_string(item.get("action"))
        and _non_empty_string(item.get("target_scope"))
        and _recommendation_dimension_allowed(item, answer_dimensions)
        and bool(_model_string_list(item.get("basis_refs")))
    )


def _recommendation_dimension_allowed(item: dict[str, Any], answer_dimensions: list[str] | None) -> bool:
    allowed = {str(value).strip() for value in answer_dimensions or [] if str(value).strip()}
    if not allowed:
        return True
    return str(item.get("answer_dimension") or "").strip() in allowed


def _all_report_refs_are_claimable(final_report: dict[str, Any], claimable_refs: list[str]) -> bool:
    allowed_refs = {ref for ref in claimable_refs if ref}
    for refs in _iter_report_ref_fields(final_report):
        if refs is None:
            continue
        if not isinstance(refs, list):
            return False
        for ref in refs:
            if not isinstance(ref, str):
                return False
            if ref.strip() not in allowed_refs:
                return False
    return True


def _iter_report_ref_fields(final_report: dict[str, Any]):
    yield final_report.get("evidence_refs")
    findings = final_report.get("findings")
    if isinstance(findings, list):
        for item in findings:
            if isinstance(item, dict):
                yield item.get("evidence_refs")
    recommendations = final_report.get("recommendations")
    if isinstance(recommendations, list):
        for item in recommendations:
            if isinstance(item, dict):
                yield item.get("basis_refs")


def _reports_missing_tool_evidence(final_report: dict[str, Any], missing_tools: list[dict[str, Any]]) -> bool:
    report_missing = _model_missing_data(final_report.get("missing_data"))
    if not report_missing:
        return False
    for item in missing_tools:
        observed_missing = _model_missing_data(item.get("missing_data"))
        if observed_missing and any(missing in report_missing for missing in observed_missing):
            continue
        tool_name = str(item.get("tool_name") or "").strip().lower()
        if tool_name and any(tool_name in missing.lower() for missing in report_missing):
            continue
        return False
    return True


def _has_unsynthesized_detail_listing(final_report: dict[str, Any]) -> bool:
    findings = final_report.get("findings")
    if not isinstance(findings, list):
        return False
    for item in findings:
        if not isinstance(item, dict):
            continue
        if _looks_like_key_value_listing(str(item.get("summary") or "")):
            return True
    return False


def _looks_like_key_value_listing(text: str) -> bool:
    normalized = " ".join(text.split())
    if not normalized:
        return False
    pairs = 0
    for token in normalized.replace("，", " ").replace(",", " ").replace("；", " ").split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key.strip() and value.strip():
            pairs += 1
    if pairs < 3:
        return False
    prefix = normalized.split(None, 1)[0].rstrip(".、")
    if prefix.isdigit():
        return True
    return pairs >= 5


def _recommendations_are_supported_by_findings(
    final_report: dict[str, Any],
    *,
    answer_dimensions: list[str] | None = None,
) -> bool:
    finding_refs = _finding_ref_sets(final_report)
    recommendations = final_report.get("recommendations")
    if not isinstance(recommendations, list) or not recommendations:
        return False
    for item in recommendations:
        if not isinstance(item, dict):
            return False
        if not _valid_recommendation_shape(item, answer_dimensions=answer_dimensions):
            return False
        refs = set(_model_string_list(item.get("basis_refs")))
        if not refs:
            return False
        if not any(refs.intersection(finding) for finding in finding_refs):
            return False
    return True


def _finding_ref_sets(final_report: dict[str, Any]) -> list[set[str]]:
    findings = final_report.get("findings")
    if not isinstance(findings, list):
        return []
    refs: list[set[str]] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        item_refs = {ref for ref in _model_string_list(item.get("evidence_refs")) if ref}
        if item_refs:
            refs.append(item_refs)
    return refs


def _recommendations_cite_any(final_report: dict[str, Any], refs: list[str]) -> bool:
    required = {ref for ref in refs if ref}
    if not required:
        return True
    recommendations = final_report.get("recommendations")
    if not isinstance(recommendations, list):
        return False
    for item in recommendations:
        if not isinstance(item, dict):
            continue
        if required.intersection(_model_string_list(item.get("basis_refs"))):
            return True
    return False


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
