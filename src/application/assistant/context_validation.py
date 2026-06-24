from __future__ import annotations

from typing import Any

from src.application.assistant.context_projection import SAFE_SLOT_KEYS


CONTEXT_VALIDATION_SCHEMA_VERSION = "om-context-validation-v1"
PLANNER_CONTEXT_USE_SCHEMA_VERSION = "om-planner-context-use-v1"
CONTEXT_USE_MODES = ("none", "carry", "refine", "override", "ambiguous")
CONTEXT_USING_MODES = {"carry", "refine", "override"}
CONTEXT_VALIDATION_STATUSES = ("passed", "blocked", "ask_clarification")

_HIDDEN_ARGUMENT_KEYS = frozenset(
    {
        "accounts_root",
        "audit_db",
        "candidate_paths",
        "candidate_reject_log_paths",
        "candidate_report_dir",
        "candidate_trace_paths",
        "config_key",
        "config_path",
        "csv_path",
        "data_config",
        "delivery",
        "delivery_mode",
        "env_file",
        "file",
        "include_service_status",
        "log_file",
        "logs_root",
        "max_notification_chars",
        "max_run_age_minutes",
        "opend_telnet_host",
        "opend_telnet_port",
        "output_dir",
        "profile_path",
        "report_dir",
        "report_path",
        "run_dir",
        "runs_root",
        "send_mode",
        "service_status",
        "state_dir",
        "timeout_sec",
        "timeout_seconds",
        "trigger_source",
        "webhook",
    }
)


def validate_context_use(
    *,
    current_user_message: str | None = None,
    context_projection: dict[str, Any] | None,
    plan_payload: dict[str, Any] | None,
    planner_manifest: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Validate a planner context-use declaration against visible context only.

    This function is deterministic and structural. It does not inspect business
    wording in the current message, infer missing facts, or choose tools.
    """
    del current_user_message
    projection = context_projection if isinstance(context_projection, dict) else {}
    plan = plan_payload if isinstance(plan_payload, dict) else {}
    context_use = _normalized_context_use(plan.get("context_use"))
    manifest = _manifest_by_tool(planner_manifest)
    steps = [dict(item) for item in plan.get("steps") or [] if isinstance(item, dict)]
    referenced_turn_ids = _string_list(context_use.get("referenced_turn_ids"))
    referenced_evidence_refs = _string_list(context_use.get("referenced_evidence_refs"))
    inherited_slots = _slot_mapping(context_use.get("inherited_slots"))
    current_slots = _slot_mapping(context_use.get("current_message_slots"))
    override_slots = _slot_mapping(context_use.get("override_slots"))
    warnings: list[dict[str, Any]] = []

    if not projection:
        warnings.append({"code": "CONTEXT_PROJECTION_MISSING", "message": "context projection was not provided"})

    tool_mismatch = _tool_compatibility_violation(steps, manifest=manifest, inherited_slots=inherited_slots)
    if tool_mismatch is not None:
        return _validation_payload(
            status="blocked",
            code="CONTEXT_TOOL_MISMATCH",
            context_use=context_use,
            warnings=warnings,
            violation=tool_mismatch,
        )

    if context_use["mode"] == "ambiguous" or context_use["requires_clarification"]:
        if steps:
            return _validation_payload(
                status="blocked",
                code="CONTEXT_CLARIFICATION_WITH_TOOL_STEPS",
                context_use=context_use,
                warnings=warnings,
                violation={"reason": "clarification_requested_with_executable_steps", "step_count": len(steps)},
            )
        return _validation_payload(
            status="ask_clarification",
            code="CONTEXT_AMBIGUOUS",
            context_use=context_use,
            warnings=warnings,
            violation={"reason": "planner_declared_ambiguity"},
        )

    ref_violation = _reference_violation(
        projection=projection,
        referenced_turn_ids=referenced_turn_ids,
        referenced_evidence_refs=referenced_evidence_refs,
    )
    if ref_violation is not None:
        return _validation_payload(
            status="blocked",
            code="CONTEXT_REF_NOT_FOUND",
            context_use=context_use,
            warnings=warnings,
            violation=ref_violation,
        )

    source_slots = _source_slots(
        projection=projection,
        referenced_turn_ids=referenced_turn_ids,
        referenced_evidence_refs=referenced_evidence_refs,
    )
    slot_violation = _slot_source_violation(
        inherited_slots=inherited_slots,
        source_slots=source_slots,
        plan_slots=_plan_safe_argument_slots(steps),
        current_slots=current_slots,
        override_slots=override_slots,
        mode=str(context_use["mode"]),
    )
    if slot_violation is not None:
        return _validation_payload(
            status="blocked",
            code=str(slot_violation.pop("code")),
            context_use=context_use,
            warnings=warnings,
            violation=slot_violation,
        )

    ambiguity = _ambiguity_violation(
        projection=projection,
        mode=str(context_use["mode"]),
        referenced_turn_ids=referenced_turn_ids,
        referenced_evidence_refs=referenced_evidence_refs,
    )
    if ambiguity is not None:
        return _validation_payload(
            status="ask_clarification",
            code="CONTEXT_AMBIGUOUS",
            context_use=context_use,
            warnings=warnings,
            violation=ambiguity,
        )

    return _validation_payload(
        status="passed",
        code="ok",
        context_use=context_use,
        warnings=warnings,
    )


def context_validation_trace(validation: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(validation, dict) or not validation:
        return {"provided": False}
    return {
        "provided": True,
        "schema_version": validation.get("schema_version"),
        "status": validation.get("status"),
        "code": validation.get("code"),
        "context_use_mode": validation.get("context_use_mode"),
        "referenced_turn_count": len(validation.get("referenced_turn_ids") or []),
        "referenced_evidence_count": len(validation.get("referenced_evidence_refs") or []),
        "warning_count": len(validation.get("warnings") or []),
    }


def _validation_payload(
    *,
    status: str,
    code: str,
    context_use: dict[str, Any],
    warnings: list[dict[str, Any]],
    violation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": CONTEXT_VALIDATION_SCHEMA_VERSION,
        "status": status if status in CONTEXT_VALIDATION_STATUSES else "blocked",
        "code": str(code or "CONTEXT_VALIDATION_FAILED"),
        "context_use_mode": context_use["mode"],
        "referenced_turn_ids": _string_list(context_use.get("referenced_turn_ids")),
        "referenced_evidence_refs": _string_list(context_use.get("referenced_evidence_refs")),
        "validated_slots": {
            "inherited": _slot_mapping(context_use.get("inherited_slots")),
            "current_message": _slot_mapping(context_use.get("current_message_slots")),
            "override": _slot_mapping(context_use.get("override_slots")),
        },
        "warnings": [dict(item) for item in warnings],
    }
    if violation:
        payload["violation"] = dict(violation)
    return payload


def _normalized_context_use(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    mode = str(raw.get("mode") or "none").strip()
    if mode not in CONTEXT_USE_MODES:
        mode = "none"
    return {
        "schema_version": str(raw.get("schema_version") or PLANNER_CONTEXT_USE_SCHEMA_VERSION),
        "mode": mode,
        "referenced_turn_ids": _string_list(raw.get("referenced_turn_ids")),
        "referenced_evidence_refs": _string_list(raw.get("referenced_evidence_refs")),
        "inherited_slots": _slot_mapping(raw.get("inherited_slots")),
        "current_message_slots": _slot_mapping(raw.get("current_message_slots")),
        "override_slots": _slot_mapping(raw.get("override_slots")),
        "requires_clarification": bool(raw.get("requires_clarification")),
        "clarification_question": raw.get("clarification_question") if raw.get("clarification_question") else None,
    }


def _manifest_by_tool(planner_manifest: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in planner_manifest or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            out[name] = item
    return out


def _tool_compatibility_violation(
    steps: list[dict[str, Any]],
    *,
    manifest: dict[str, dict[str, Any]],
    inherited_slots: dict[str, list[Any]],
) -> dict[str, Any] | None:
    planned_tools = {str(step.get("tool_name") or "").strip() for step in steps}
    planned_tools.discard("")
    for tool_name in sorted(planned_tools):
        if tool_name not in manifest:
            return {"reason": "tool_not_in_planner_manifest", "tool_name": tool_name}
    for step in steps:
        tool_name = str(step.get("tool_name") or "").strip()
        arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
        hidden = _hidden_argument_paths(arguments)
        if hidden:
            return {"reason": "hidden_injected_arguments", "tool_name": tool_name, "banned_arguments": hidden}
        allowed = _allowed_arguments(manifest.get(tool_name))
        extra = sorted(str(key) for key in arguments if allowed and str(key) not in allowed)
        if extra:
            return {
                "reason": "argument_not_in_planner_manifest",
                "tool_name": tool_name,
                "extra_arguments": extra,
                "allowed_arguments": sorted(allowed),
            }
    declared_slot_keys = set(inherited_slots)
    compatible_slot_keys = set(SAFE_SLOT_KEYS)
    for step in steps:
        compatible_slot_keys.update(_allowed_arguments(manifest.get(str(step.get("tool_name") or "").strip())))
    incompatible = sorted(key for key in declared_slot_keys if key not in compatible_slot_keys)
    if incompatible:
        return {"reason": "inherited_slot_not_allowed_for_planner", "slots": incompatible}
    return None


def _allowed_arguments(tool_meta: dict[str, Any] | None) -> set[str]:
    schema = tool_meta.get("input_schema") if isinstance(tool_meta, dict) else None
    if not isinstance(schema, dict):
        return set()
    return {str(key) for key in schema if str(key).strip()}


def _hidden_argument_paths(value: Any, *, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        hits: list[str] = []
        for raw_key, item in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            if key in _HIDDEN_ARGUMENT_KEYS:
                hits.append(path)
            hits.extend(_hidden_argument_paths(item, prefix=path))
        return hits
    if isinstance(value, list):
        hits = []
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            hits.extend(_hidden_argument_paths(item, prefix=path))
        return hits
    return []


def _reference_violation(
    *,
    projection: dict[str, Any],
    referenced_turn_ids: list[str],
    referenced_evidence_refs: list[str],
) -> dict[str, Any] | None:
    turns = _turns_by_id(projection)
    missing_turns = sorted(turn_id for turn_id in referenced_turn_ids if turn_id not in turns)
    if missing_turns:
        return {"reason": "referenced_turn_missing", "missing_turn_ids": missing_turns}
    available_refs = set(_evidence_by_id(projection))
    refs_inside_referenced_turns: set[str] = set()
    for turn_id in referenced_turn_ids:
        refs_inside_referenced_turns.update(_string_list(turns.get(turn_id, {}).get("evidence_refs")))
    missing_refs = sorted(
        ref_id
        for ref_id in referenced_evidence_refs
        if ref_id not in available_refs and ref_id not in refs_inside_referenced_turns
    )
    if missing_refs:
        return {"reason": "referenced_evidence_missing", "missing_evidence_refs": missing_refs}
    return None


def _slot_source_violation(
    *,
    inherited_slots: dict[str, list[Any]],
    source_slots: dict[str, list[Any]],
    plan_slots: dict[str, list[Any]],
    current_slots: dict[str, list[Any]],
    override_slots: dict[str, list[Any]],
    mode: str,
) -> dict[str, Any] | None:
    for key, values in inherited_slots.items():
        available = source_slots.get(key, [])
        if not _values_subset(values, available):
            return {
                "code": "CONTEXT_SLOT_NOT_AVAILABLE",
                "reason": "declared_inherited_slot_not_available",
                "slot": key,
                "declared_values": values,
                "available_values": available,
            }
    for key, values in plan_slots.items():
        if key in current_slots or key in override_slots:
            continue
        if key in source_slots and _values_overlap(values, source_slots[key]) and not _values_subset(values, inherited_slots.get(key, [])):
            return {
                "code": "CONTEXT_SLOT_NOT_AVAILABLE",
                "reason": "plan_uses_visible_context_slot_without_declaration",
                "slot": key,
                "plan_values": values,
                "available_values": source_slots[key],
            }
    for key, current_values in current_slots.items():
        plan_values = plan_slots.get(key, [])
        if plan_values and not _values_subset(plan_values, current_values + override_slots.get(key, [])):
            return {
                "code": "CONTEXT_CURRENT_MESSAGE_OVERRIDDEN",
                "reason": "plan_uses_value_conflicting_with_current_message",
                "slot": key,
                "current_values": current_values,
                "plan_values": plan_values,
            }
        inherited_values = inherited_slots.get(key, [])
        if inherited_values and not _values_subset(inherited_values, current_values) and mode != "override":
            return {
                "code": "CONTEXT_CURRENT_MESSAGE_OVERRIDDEN",
                "reason": "inherited_slot_conflicts_with_current_message",
                "slot": key,
                "current_values": current_values,
                "inherited_values": inherited_values,
            }
    return None


def _ambiguity_violation(
    *,
    projection: dict[str, Any],
    mode: str,
    referenced_turn_ids: list[str],
    referenced_evidence_refs: list[str],
) -> dict[str, Any] | None:
    if mode not in CONTEXT_USING_MODES:
        return None
    has_reference = bool(referenced_turn_ids or referenced_evidence_refs)
    budget = projection.get("budget") if isinstance(projection.get("budget"), dict) else {}
    if bool(budget.get("truncated")) and not has_reference:
        return {"reason": "context_projection_truncated_without_reference"}
    if mode == "carry" and not has_reference:
        overlap = _overlapping_recent_turn_slots(projection)
        if overlap:
            return {"reason": "carry_context_without_reference_and_overlapping_slots", "overlap": overlap}
    return None


def _source_slots(
    *,
    projection: dict[str, Any],
    referenced_turn_ids: list[str],
    referenced_evidence_refs: list[str],
) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    turns = _turns_by_id(projection)
    for turn_id in referenced_turn_ids:
        _merge_slots(out, turns.get(turn_id, {}).get("safe_slots"))
    evidence = _evidence_by_id(projection)
    for ref_id in referenced_evidence_refs:
        _merge_slots(out, evidence.get(ref_id, {}).get("safe_slots"))
    for tool in projection.get("recent_successful_tools") or []:
        if not isinstance(tool, dict):
            continue
        tool_refs = set(_string_list(tool.get("evidence_refs")))
        if tool_refs.intersection(referenced_evidence_refs):
            _merge_slots(out, tool.get("safe_slots"))
    return out


def _turns_by_id(projection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for turn in projection.get("recent_turns") or []:
        if isinstance(turn, dict):
            turn_id = str(turn.get("turn_id") or "").strip()
            if turn_id:
                out[turn_id] = turn
    return out


def _evidence_by_id(projection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ref in projection.get("available_evidence_refs") or []:
        if isinstance(ref, dict):
            ref_id = str(ref.get("ref_id") or "").strip()
            if ref_id:
                out[ref_id] = ref
    return out


def _plan_safe_argument_slots(steps: list[dict[str, Any]]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for step in steps:
        tool_name = str(step.get("tool_name") or "").strip()
        arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
        for key, value in arguments.items():
            slot_key = str(key)
            if slot_key in SAFE_SLOT_KEYS:
                for item in _value_list(value):
                    _add_slot(out, slot_key, item)
        if tool_name == "symbol_edit":
            out = _merge_plan_slots(out, _symbol_edit_setting_slots(arguments))
    return out


def _symbol_edit_setting_slots(arguments: dict[str, Any]) -> dict[str, list[Any]]:
    sets = arguments.get("set") if isinstance(arguments.get("set"), dict) else {}
    out: dict[str, list[Any]] = {}
    for raw_path, value in sets.items():
        path = str(raw_path or "").strip()
        if not path:
            continue
        _add_slot(out, "setting_path", path)
        parts = [part for part in path.split(".") if part]
        if parts:
            _add_slot(out, "setting_field", parts[-1])
        if len(parts) >= 2:
            _add_slot(out, "strategy", parts[0])
        if isinstance(value, (str, int, float, bool)) or value is None:
            _add_slot(out, "setting_new_value", value)
    return out


def _merge_plan_slots(left: dict[str, list[Any]], right: dict[str, list[Any]]) -> dict[str, list[Any]]:
    out = {key: list(values) for key, values in left.items()}
    for key, values in right.items():
        for value in values:
            _add_slot(out, key, value)
    return out


def _overlapping_recent_turn_slots(projection: dict[str, Any]) -> dict[str, list[Any]]:
    slot_to_values: dict[str, list[Any]] = {}
    slot_to_turns: dict[str, set[str]] = {}
    for turn in projection.get("recent_turns") or []:
        if not isinstance(turn, dict):
            continue
        turn_id = str(turn.get("turn_id") or "").strip()
        slots = _slot_mapping(turn.get("safe_slots"))
        for key, values in slots.items():
            if values:
                slot_to_turns.setdefault(key, set()).add(turn_id)
                for value in values:
                    _add_slot(slot_to_values, key, value)
    return {
        key: slot_to_values.get(key, [])
        for key, turns in slot_to_turns.items()
        if len(turns) > 1 and len(slot_to_values.get(key, [])) > 1
    }


def _slot_mapping(value: Any) -> dict[str, list[Any]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[Any]] = {}
    for key, raw_values in value.items():
        slot_key = str(key or "").strip()
        if not slot_key:
            continue
        for item in _value_list(raw_values):
            _add_slot(out, slot_key, item)
    return out


def _merge_slots(out: dict[str, list[Any]], value: Any) -> None:
    for key, values in _slot_mapping(value).items():
        for item in values:
            _add_slot(out, key, item)


def _add_slot(out: dict[str, list[Any]], key: str, value: Any) -> None:
    if value in ("", None):
        return
    bucket = out.setdefault(str(key), [])
    if value not in bucket:
        bucket.append(value)


def _value_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, tuple):
        values = list(value)
    else:
        values = [value]
    return [item for item in values if item not in ("", None, [], {})]


def _string_list(value: Any) -> list[str]:
    values = value if isinstance(value, list) else ([] if value is None else [value])
    out: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _values_subset(left: list[Any], right: list[Any]) -> bool:
    right_norm = {_normalized_value(item) for item in right}
    return all(_normalized_value(item) in right_norm for item in left)


def _values_overlap(left: list[Any], right: list[Any]) -> bool:
    right_norm = {_normalized_value(item) for item in right}
    return any(_normalized_value(item) in right_norm for item in left)


def _normalized_value(value: Any) -> str:
    return str(value).strip().lower()


__all__ = [
    "CONTEXT_VALIDATION_SCHEMA_VERSION",
    "CONTEXT_VALIDATION_STATUSES",
    "validate_context_use",
    "context_validation_trace",
]
