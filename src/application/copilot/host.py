from __future__ import annotations

import hashlib
import importlib
import json
import re
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import date
from threading import Lock
from typing import Any, Callable, Mapping

from src.application.agent_tool_registry import get_tool_definition
from src.application.agent_tool_config import resolve_runtime_config_path
from src.application.copilot import tools as copilot_tools
from src.application.copilot.control_handoff import (
    CONTROL_PREVIEW_TOOL,
    build_control_preview_request,
    control_preview_tool_description,
)
from src.application.copilot.contracts import AppResult, ExecutionContract, SceneManifest, new_id
from src.application.copilot.event_store import CopilotEventLog
from src.application.copilot.host_store import CopilotHostStore
from src.application.copilot.model_config import PiModelSettings
from src.application.copilot.result_admission import admit_result_with_decision, admit_submit_answer
from src.application.copilot.scene import build_scene_manifest, scene_policy_rejection_reason
from src.application.research.redaction import redact_value
from src.infrastructure.pi_agent_process import (
    derive_pi_local_session_id,
    derive_pi_session_id,
    run_pi_agent,
)


CancellationChecker = Callable[[], bool]
FixtureObservationLoader = Callable[[str | None], list[dict[str, Any]]]
_SESSION_LOCK = Lock()
_RUNNING_SESSIONS: set[str] = set()
_PROCESS_ERROR_CODES = {
    "CONFIG_ERROR": "CONFIG_ERROR",
    "PI_RUNTIME_UNAVAILABLE": "DEPENDENCY_MISSING",
    "MODEL_ERROR": "MODEL_ERROR",
    "PI_PROCESS_TIMEOUT": "MODEL_ERROR",
    "PI_PROCESS_EXITED": "MODEL_ERROR",
    "TOOL_BRIDGE_ERROR": "TOOL_ERROR",
    "BUDGET_EXHAUSTED": "BUDGET_EXHAUSTED",
    "ANSWER_ADMISSION_FAILED": "ANSWER_ADMISSION_FAILED",
    "CANCELLED": "CANCELLED",
    "SESSION_ERROR": "INTERNAL_ERROR",
    "PROTOCOL_ERROR": "INTERNAL_ERROR",
    "INTERNAL_ERROR": "INTERNAL_ERROR",
}
_OPTION_PERFORMANCE_CUTOFF = re.compile(
    r"^\s*截至\s*(?P<date>\d{4}-\d{2}-\d{2})\s*的\s*"
    r"(?P<month>0?[1-9]|1[0-2])月期权收益率"
    r"(?:\s*[，,]\s*总计)?(?:\s*[，,]\s*不分账号)?\s*[。.]?\s*$"
)
_OPTION_PERFORMANCE_CUTOFF_INDICATOR = re.compile(
    r"截至|截止到|截止|(?i:\bas\s+of\b)|"
    r"(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?!\d)|"
    r"(?<!\d)\d{4}年\d{1,2}月\d{1,2}日|"
    r"(?<!\d)\d{1,2}月\d{1,2}日"
)


def _constrain_option_performance_cutoff(
    payload: dict[str, Any],
    *,
    user_message: str,
) -> str | None:
    match = _OPTION_PERFORMANCE_CUTOFF.fullmatch(user_message)
    if match is not None:
        try:
            cutoff = date.fromisoformat(match.group("date"))
        except ValueError:
            return "option performance cutoff is not authorized by the current message"
        if (
            cutoff.month != int(match.group("month"))
            or payload.get("period") != "mtd"
            or payload.get("as_of_date") != cutoff.isoformat()
        ):
            return "option performance cutoff is not authorized by the current message"
        return None
    if payload.get("period") != "mtd" or payload.get("as_of_date") in (None, ""):
        return None
    if _OPTION_PERFORMANCE_CUTOFF_INDICATOR.search(user_message):
        return "option performance cutoff is not authorized by the current message"
    payload.pop("as_of_date", None)
    return None


@contextmanager
def session_run_slot(
    session_key: str,
    *,
    host_store: CopilotHostStore | None = None,
    ttl_seconds: int = 300,
):
    if host_store is not None:
        lease_id = new_id("lease")
        entered = host_store.acquire_session_run(session_key, lease_id, ttl_seconds=ttl_seconds)
        try:
            yield entered
        finally:
            if entered:
                host_store.release_session_run(session_key, lease_id)
        return
    with _SESSION_LOCK:
        if session_key in _RUNNING_SESSIONS:
            yield False
            return
        _RUNNING_SESSIONS.add(session_key)
    try:
        yield True
    finally:
        with _SESSION_LOCK:
            _RUNNING_SESSIONS.discard(session_key)


@contextmanager
def host_lane_slot(
    lane: str,
    *,
    host_store: CopilotHostStore | None,
    limit: int,
    ttl_seconds: int,
):
    if host_store is None:
        yield True
        return
    lease_id = new_id("lane")
    entered = host_store.acquire_lane(lane, lease_id, limit=limit, ttl_seconds=ttl_seconds)
    try:
        yield entered
    finally:
        if entered:
            host_store.release_lane(lane, lease_id)


def run_contract(
    contract: ExecutionContract,
    *,
    model_settings: PiModelSettings | None = None,
    debug: dict[str, Any] | None = None,
    process_environ: Mapping[str, str] | None = None,
    is_cancelled: CancellationChecker | None = None,
    fixture_observations_loader: FixtureObservationLoader | None = None,
    host_store: CopilotHostStore | None = None,
    session_key: str | None = None,
    control_preview_specs: tuple[dict[str, Any], ...] = (),
    resumed_from: str | None = None,
    recovered_observations: tuple[dict[str, Any], ...] = (),
    enabled_optional_toolsets: frozenset[str] = frozenset(),
    tool_loading_mode: str = "eager",
) -> AppResult:
    run_id = new_id("run")
    if host_store is not None:
        host_store.start_run(
            run_id,
            contract=contract,
            session_key=session_key,
            resumed_from=resumed_from,
        )
    event_log = CopilotEventLog(
        run_id,
        sink=host_store.append_event if host_store is not None else None,
    )
    run_lock = Lock()
    tool_events_open = True
    finalized = False

    def record_event(
        event_type: str,
        payload: dict[str, Any],
        visible_ref: str | None = None,
    ) -> None:
        with run_lock:
            if tool_events_open and not finalized:
                event_log.record(event_type, payload, visible_ref)

    def record_terminal_event(event_type: str, payload: dict[str, Any]) -> None:
        with run_lock:
            if not finalized:
                event_log.record(event_type, payload)

    def close_host_admission(status: str) -> str | None:
        if host_store is None:
            return None
        try:
            if status == "cancelled":
                host_store.request_cancel(run_id)
            return host_store.claim_admission_decision(run_id, "discard")
        except Exception:
            return None

    def finish(result: AppResult) -> AppResult:
        nonlocal finalized, tool_events_open
        with run_lock:
            if finalized:
                return replace(result, events=list(event_log.events))
            if result.status not in {"answered", "control_requested"}:
                winner = close_host_admission(result.status)
                if winner == "cancel" and result.status != "cancelled":
                    result = replace(
                        result,
                        status="cancelled",
                        user_response="Copilot 运行已取消。",
                        error={"code": "CANCELLED"},
                        control_request=None,
                        ok=False,
                    )
                    event_log.record("run_cancelled", {"reason": "cancellation_requested"})
            tool_events_open = False
            event_log.record_final_result(result)
            completed = replace(result, events=list(event_log.events))
            if host_store is not None:
                host_store.finish_run(completed)
            finalized = True
            return completed

    record_event(
        "contract_received",
        {
            "contract_id": contract.contract_id,
            "request_id": contract.request_id,
            "scene": contract.scene_name,
            "execution_environment": contract.execution_environment,
            "read_only": contract.policy.get("read_only") is True,
        },
    )
    rejection = _contract_rejection_reason(contract)
    if rejection:
        return finish(
            AppResult(
                status="not_ready",
                user_response="Copilot 未执行请求，因为执行合同未通过只读策略校验。",
                error={"code": "CONTRACT_REJECTED", "reason": rejection},
                request_id=contract.request_id,
                contract_id=contract.contract_id,
                run_id=run_id,
                events=list(event_log.events),
                decision_trace=contract.decision_trace,
                ok=False,
            ),
        )
    projected_control_specs = (
        control_preview_specs
        if contract.execution_environment == "channel"
        and str(contract.input.get("authenticated_channel") or "").strip()
        and str(contract.input.get("authenticated_sender_id") or "").strip()
        else ()
    )
    try:
        # Tool loading is a canonical assistant.copilot setting.  The
        # prepared contract carries the already validated value; environment
        # variables are deliberately not a second authority.
        configured_mode = str(tool_loading_mode or "eager").strip().lower()
        scene_manifest = build_scene_manifest(
            contract,
            run_id,
            enabled_optional_toolsets=enabled_optional_toolsets,
            tool_loading_mode=configured_mode,
        )
        manifest = _manifest_with_tool_descriptions(
            scene_manifest,
            control_preview_specs=projected_control_specs,
        )
        system_prompt, runtime_context, user_message = _manifest_prompt_parts(
            manifest,
            contract,
        )
        pi_session_id = _pi_session_id(contract, session_key)
    except Exception:
        record_event("scene_preparation_failed", {"reason": "manifest_error"})
        return finish(
            AppResult(
                status="failed",
                user_response="Copilot 未能准备只读执行场景。",
                error={"code": "SCENE_PREPARATION_FAILED"},
                request_id=contract.request_id,
                contract_id=contract.contract_id,
                run_id=run_id,
                events=list(event_log.events),
                decision_trace=contract.decision_trace,
                ok=False,
            ),
        )

    record_event("scene_prepared", _scene_prepared_payload(manifest))
    if model_settings is None:
        return finish(
            _failed_result(
                contract,
                run_id,
                event_log,
                code="MODEL_REQUIRED",
                response="Copilot 模型未配置，本次没有调用工具。",
            )
        )

    fixture_id = _fixture_id(contract)
    recovery = list(recovered_observations)
    if fixture_id is not None:
        try:
            loader = fixture_observations_loader or _default_fixture_observations
            fixture_items = [dict(item) for item in loader(fixture_id)]
        except Exception:
            fixture_items = [
                {
                    "tool_name": "fixture",
                    "ok": False,
                    "status": "failed",
                    "error": "FIXTURE_ERROR",
                    "message": "fixture observations could not be loaded",
                }
            ]
        for item in fixture_items:
            record_event(
                "fixture_observation",
                redact_value(item),
                str(item.get("ref") or "") or None,
            )
        recovery.extend(fixture_items)

    limits = _process_limits(
        manifest,
    )
    tool_loading_mode = manifest.tool_loading_mode
    catalog_snapshot = deepcopy(manifest.catalog_snapshot)
    start_payload = {
        "execution_environment": contract.execution_environment,
        "session_id": pi_session_id,
        "system_prompt": system_prompt,
        "runtime_context": runtime_context,
        "user_message": user_message,
        "model": _effective_model_payload(model_settings, limits["timeout_seconds"]),
        "tools": _provider_tools(manifest),
        "tool_loading_mode": tool_loading_mode,
        "tool_catalog": deepcopy(manifest.tool_catalog),
        "catalog_hash": manifest.catalog_hash,
        "catalog_snapshot": catalog_snapshot,
        "limits": limits,
        "recovered_observations": _bounded_recovered_observations(tuple(recovery)),
        "debug": deepcopy(debug) if debug is not None else None,
    }

    observation_count = 0
    evidence_registry: dict[str, dict[str, Any]] = {}
    active_evidence_tokens = 0
    turn_count = 0
    latest_usage: dict[str, Any] = {}
    latest_retry_count = 0
    retained_result: AppResult | None = None
    retained_decision: str | None = None
    approved_answer_text: str | None = None
    approved_answer_hash: str | None = None
    last_answer_rejection_reason: str | None = None
    local_admission_state = "open"

    def cancellation_requested() -> bool:
        nonlocal local_admission_state
        requested = bool(is_cancelled and is_cancelled())
        if host_store is not None:
            if requested:
                host_store.request_cancel(run_id)
            return host_store.is_cancel_requested(run_id)
        with run_lock:
            if requested and local_admission_state == "open":
                local_admission_state = "cancel"
            return local_admission_state == "cancel"

    def on_process_event(event: dict[str, Any]) -> None:
        nonlocal latest_retry_count, latest_usage, turn_count
        event_type = str(event.get("event_type") or "")
        data = dict(event.get("data") or {})
        if event_type == "agent_start":
            record_event("agent_started", {})
        elif event_type == "turn_start":
            turn_count += 1
            record_event("model_turn_started", {"iteration": turn_count})
        elif event_type == "model_turn_completed":
            latest_usage = dict(data.get("usage_total") or {})
            latest_retry_count = max(
                latest_retry_count,
                int(data.get("model_retry_count") or 0),
            )
            record_event(
                "model_turn_completed",
                {
                    "iteration": turn_count,
                    "stop_reason": str(data.get("stop_reason") or ""),
                    "attempt_count": int(data.get("attempt_count") or 0),
                    "model_retry_count": latest_retry_count,
                    "usage": dict(data.get("usage") or {}),
                    "usage_total": latest_usage,
                },
            )
        elif event_type == "context_compaction_committed":
            latest_usage = dict(data.get("usage_total") or {})
            record_event(
                "context_compacted",
                {
                    "compaction_count": int(data.get("compaction_count") or 0),
                    "usage_total": latest_usage,
                },
            )
        elif event_type == "forced_final_activated":
            record_event(
                "agent_budget_fallback",
                {"reason": str(data.get("reason") or "")},
            )
        elif event_type == "context_budget_checked":
            record_event(
                "context_budget_checked",
                {
                    "estimated_input_tokens": int(data.get("estimated_input_tokens") or 0),
                    "effective_capacity_tokens": int(data.get("effective_capacity_tokens") or 0),
                    "decision": str(data.get("decision") or ""),
                    "compaction_target": str(data.get("compaction_target") or ""),
                },
            )
        elif event_type == "catalog_loaded":
            record_event(
                "catalog_loaded",
                {
                    "catalog_hash": str(data.get("catalog_hash") or ""),
                    "tool_count": int(data.get("tool_count") or 0),
                    "description_chars": int(data.get("description_chars") or 0),
                    "estimated_tokens": int(data.get("estimated_tokens") or 0),
                },
            )
        elif event_type == "answer_admission":
            record_event(
                "answer_admission",
                {
                    "status": str(data.get("status") or ""),
                    "evidence_count": int(data.get("evidence_count") or 0),
                    "repair_count": int(data.get("repair_count") or 0),
                    "banner_present": data.get("banner_present") is True,
                },
            )
        elif event_type == "agent_end":
            record_event(
                "agent_terminated",
                {
                    "reason": "completed",
                    "model_turn_count": turn_count,
                    "model_retry_count": latest_retry_count,
                    "usage_total": latest_usage,
                },
            )

    def on_tool_call(call: dict[str, Any]) -> dict[str, Any]:
        nonlocal observation_count, active_evidence_tokens, last_answer_rejection_reason
        tool_name = str(call.get("tool_name") or "")
        call_id = str(call.get("call_id") or "")
        arguments = dict(call.get("arguments") or {})

        def unavailable(code: str, message: str) -> dict[str, Any]:
            observation = _tool_error(tool_name, code, message)
            if tool_name in {"tool_directory", "submit_answer"}:
                return {"observation": observation}
            if tool_name == CONTROL_PREVIEW_TOOL:
                return {"observation": observation, "control_request": None}
            return observation

        if cancellation_requested():
            return unavailable("CANCELLED", "run cancelled before tool execution")
        with run_lock:
            if not tool_events_open or finalized:
                return unavailable("CANCELLED", "run is no longer active")

        if tool_name == "tool_directory":
            requested_hash = str(arguments.get("catalog_hash") or "")
            names = arguments.get("tool_names")
            selected = [str(item) for item in names] if isinstance(names, list) else []
            catalog_by_name = {str(item["name"]): item for item in manifest.tool_catalog}
            selected_toolsets = {str(catalog_by_name[name]["toolset"]) for name in selected if name in catalog_by_name}
            event_log.record(
                "tool_call",
                {
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "catalog_hash": requested_hash,
                    "selected_tool_names": selected,
                    "selected_toolsets": sorted(selected_toolsets),
                },
            )
            valid = (
                requested_hash == manifest.catalog_hash
                and bool(selected)
                and len(selected) == len(set(selected))
                and all(name in catalog_by_name for name in selected)
                and len(selected) <= 6
                and len(selected_toolsets) <= 2
            )
            if not valid:
                observation = _tool_error(
                    tool_name,
                    "POLICY_ERROR",
                    "tool selection is not authorized",
                )
                event_log.record(
                    "tool_result",
                    {
                        **observation,
                        "tool_call_id": call_id,
                        "selected_tool_names": selected,
                        "selected_toolsets": sorted(selected_toolsets),
                    },
                )
                return {"observation": observation}
            frozen_by_name = {
                str(item.get("name")): item for item in manifest.catalog_snapshot
            }
            schema_items = []
            for name in selected:
                frozen = frozen_by_name.get(name)
                if frozen is None or name not in manifest.allowed_tools:
                    return {"observation": _tool_error(tool_name, "POLICY_ERROR", "tool selection is not authorized")}
                schema_items.append({
                    "name": name,
                    "description": frozen["description"],
                    "input_schema": frozen["input_schema"],
                })
            schema_json = json.dumps(schema_items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            schema_hash = "sha256:" + hashlib.sha256(schema_json.encode("utf-8")).hexdigest()
            event_log.record(
                "tool_result",
                {
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "ok": True,
                    "status": "activated",
                    "catalog_hash": manifest.catalog_hash,
                    "schema_hash": schema_hash,
                    "selected_tool_names": selected,
                    "selected_toolsets": sorted(selected_toolsets),
                },
            )
            return {
                "observation": {"ok": True, "status": "activated", "active_tool_names": selected},
                "tool_activation": {
                    "catalog_hash": manifest.catalog_hash,
                    "schema_hash": schema_hash,
                    "tools": schema_items,
                },
            }

        if tool_name == "submit_answer":
            claims = arguments.get("claims")
            referenced_ids: set[str] = set()
            if isinstance(claims, list):
                for claim in claims:
                    claim_ids = claim.get("observation_ids") if isinstance(claim, dict) else None
                    if isinstance(claim_ids, list):
                        referenced_ids.update(
                            item for item in claim_ids if isinstance(item, str)
                        )
            observation_ids = sorted(referenced_ids)
            event_log.record(
                "tool_call",
                {
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "mode": str(arguments.get("mode") or ""),
                    "status": str(arguments.get("status") or ""),
                    "referenced_observation_ids": observation_ids,
                },
            )
            admitted_answer = admit_submit_answer(arguments, evidence_registry)
            approved = admitted_answer.get("approved_answer")
            if isinstance(approved, dict):
                nonlocal approved_answer_text, approved_answer_hash
                approved_answer_text = str(approved.get("text") or "")
                approved_answer_hash = str(approved.get("text_sha256") or "")
            observation = admitted_answer.get("observation")
            rejection_reason = (
                str(observation.get("reason") or "")
                if isinstance(observation, dict)
                else "invalid_result"
            )
            last_answer_rejection_reason = rejection_reason or None
            event_log.record(
                "tool_result",
                {
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "ok": bool(isinstance(observation, dict) and observation.get("ok") is True),
                    "status": str(
                        observation.get("status")
                        if isinstance(observation, dict)
                        else "failed"
                    ),
                    "referenced_observation_ids": observation_ids,
                    **({"reason": rejection_reason} if rejection_reason else {}),
                    **(
                        {"approved_answer_hash": approved_answer_hash}
                        if approved_answer_hash
                        else {}
                    ),
                },
            )
            return admitted_answer

        def bridge_observation(observation: dict[str, Any]) -> dict[str, Any]:
            if tool_name == CONTROL_PREVIEW_TOOL:
                return {"observation": observation, "control_request": None}
            return observation

        def reject(code: str, message: str) -> dict[str, Any]:
            nonlocal observation_count
            observation_count += 1
            observation = {
                **_tool_error(tool_name, code, message),
                "ref": new_id("obv"),
            }
            event_log.record(
                "tool_result",
                {**observation, "tool_call_id": call_id},
                observation["ref"],
            )
            return observation

        if cancellation_requested():
            return bridge_observation(
                _tool_error(tool_name, "CANCELLED", "run cancelled before tool execution")
            )
        with run_lock:
            if not tool_events_open or finalized:
                return bridge_observation(
                    _tool_error(tool_name, "CANCELLED", "run is no longer active")
                )
            if tool_name == CONTROL_PREVIEW_TOOL:
                event_log.record(
                    "tool_call",
                    {
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "tool_input": redact_value(arguments),
                    },
                )
                control_request, control_error = build_control_preview_request(
                    arguments,
                    user_message=str(contract.input.get("user_message") or ""),
                    specs=projected_control_specs,
                )
                if control_error or control_request is None:
                    return {
                        "observation": reject(
                            "INVALID_ACTION",
                            "control preview request is invalid",
                        ),
                        "control_request": None,
                    }
                observation_count += 1
                observation = {"ok": True, "status": "preview_requested"}
                ref = new_id("obv")
                event_log.record(
                    "tool_result",
                    {
                        **observation,
                        "ref": ref,
                        "tool_name": tool_name,
                        "tool_call_id": call_id,
                    },
                    ref,
                )
                return {
                    "observation": observation,
                    "control_request": control_request,
                }
            definition = get_tool_definition(tool_name)
            if (
                tool_name not in manifest.allowed_tools
                or definition is None
                or not definition.is_pure_read()
            ):
                return reject("POLICY_ERROR", "tool is outside the Host read-only allowlist")
            payload, payload_error = copilot_tools.build_tool_payload(
                tool_name,
                arguments,
                static_payloads=manifest.tool_static_payloads,
                fixed_input=manifest.fixed_tool_input,
            )
            if payload_error or payload is None:
                return reject(
                    "INPUT_ERROR",
                    payload_error or "tool input could not be prepared",
                )
            if tool_name == "option_performance_report":
                cutoff_error = _constrain_option_performance_cutoff(
                    payload,
                    user_message=str(contract.input.get("user_message") or ""),
                )
                if cutoff_error:
                    return reject("INPUT_ERROR", cutoff_error)
            event_log.record(
                "tool_call",
                copilot_tools.audit_tool_event_payload(
                    {
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "tool_input": payload,
                    }
                ),
            )

        try:
            response = copilot_tools.call_read_tool(
                tool_name,
                payload,
                allowed_tools=tuple(manifest.allowed_tools),
            )
        except SystemExit:
            response = {
                "ok": False,
                "error": {"code": "CONFIG_ERROR", "message": "tool configuration rejected"},
            }
        except Exception:
            response = {
                "ok": False,
                "error": {"code": "TOOL_EXCEPTION", "message": "tool raised an exception"},
            }
        try:
            observation = copilot_tools.compact_observation(tool_name, response, payload)
        except Exception:
            observation = _tool_error(
                tool_name,
                "OBSERVATION_ERROR",
                "tool result could not be normalized",
            )
        if cancellation_requested():
            return _tool_error(tool_name, "CANCELLED", "run cancelled during tool execution")
        # Failed reads are visible diagnostics but never become evidence.
        if not isinstance(observation, dict) or observation.get("ok") is not True:
            failed_observation = redact_value(
                dict(observation)
                if isinstance(observation, dict)
                else _tool_error(
                    tool_name,
                    "OBSERVATION_ERROR",
                    "tool result could not be normalized",
                )
            )
            with run_lock:
                if tool_events_open and not finalized:
                    observation_count += 1
                    failed_observation.setdefault("ref", new_id("obv"))
                    failed_observation.setdefault("tool_name", tool_name)
                    if (
                        copilot_tools.conservative_json_tokens(failed_observation)
                        > copilot_tools.MAX_OBSERVATION_TOKENS
                    ):
                        failed_observation = copilot_tools.bounded_failed_observation(
                            failed_observation,
                            tool_name=tool_name,
                        )
                    event_log.record(
                        "tool_result",
                        copilot_tools.audit_tool_event_payload(
                            {
                                **failed_observation,
                                "tool_call_id": call_id,
                                "tool_input": payload,
                            }
                        ),
                        str(failed_observation.get("ref") or "") or None,
                    )
            return failed_observation
        with run_lock:
            if not tool_events_open or finalized:
                return _tool_error(tool_name, "CANCELLED", "run is no longer active")
            observation_count += 1
            observation = copilot_tools.redact_model_observation(dict(observation))
            observation.setdefault("ref", new_id("obv"))
            observation.setdefault("tool_name", tool_name)
            definition = get_tool_definition(tool_name)
            contract_payload = definition.resolve_output_contract(payload) if definition is not None else {}
            observation["argument_hash"] = "sha256:" + hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            observation["output_contract_version"] = str(contract_payload.get("schema_version") or "unknown")
            evidence_tokens = copilot_tools.conservative_json_tokens(observation)
            if active_evidence_tokens + evidence_tokens > 20_000:
                observation = copilot_tools.bounded_narrowing_observation(
                    observation,
                    tool_name=tool_name,
                    message="当前请求的活动证据已达上限，请缩小账户、时间、标的或结果范围后重试。",
                    warning="active evidence budget exceeded",
                    minimal=True,
                )
            elif evidence_tokens > copilot_tools.MAX_OBSERVATION_TOKENS:
                observation = copilot_tools.bounded_narrowing_observation(
                    observation,
                    tool_name=tool_name,
                    message="结果超过单次证据预算，请缩小账户、时间、标的或结果范围后重试。",
                    warning="bounded projection requires narrowing",
                    minimal=True,
                )
            observation["content_hash"] = _observation_content_hash(observation)
            if (
                copilot_tools.conservative_json_tokens(observation)
                > copilot_tools.MAX_OBSERVATION_TOKENS
            ):
                observation = copilot_tools.bounded_narrowing_observation(
                    observation,
                    tool_name=tool_name,
                    message="结果超过单次证据预算，请缩小账户、时间、标的或结果范围后重试。",
                    warning="bounded projection requires narrowing",
                    minimal=True,
                )
                observation["content_hash"] = _observation_content_hash(observation)
            evidence_tokens = copilot_tools.conservative_json_tokens(observation)
            event_log.record(
                "tool_result",
                copilot_tools.audit_tool_event_payload(
                    {
                        **observation,
                        "tool_call_id": call_id,
                        "tool_input": payload,
                    }
                ),
                str(observation.get("ref") or "") or None,
            )
            # The small fail-closed narrowing observation remains admissible as
            # a diagnostic judgment.  Only its discarded full projection is
            # excluded from the active-evidence token counter.
            evidence_registry[str(observation.get("ref"))] = {
                "ok": True,
                "authorized_read": True,
                "observation_status": str(observation.get("status") or "unknown"),
                "coverage": observation.get("coverage", "unknown"),
                "freshness": observation.get("freshness", "unknown"),
                "as_of": observation.get("as_of"),
                "scope": observation.get("scope", "point"),
                "tool_name": observation.get("tool_name"),
                "argument_hash": observation.get("argument_hash"),
                "output_contract_version": observation.get("output_contract_version"),
                "content_hash": observation.get("content_hash"),
            }
            active_evidence_tokens += evidence_tokens
            return observation

    def on_proposed(proposal: dict[str, Any]) -> str:
        nonlocal local_admission_state, retained_decision, retained_result
        if approved_answer_text is not None:
            proposed_text = str(proposal.get("text") or "")
            proposed_hash = "sha256:" + hashlib.sha256(proposed_text.encode("utf-8")).hexdigest()
            if proposed_text != approved_answer_text or proposed_hash != approved_answer_hash:
                record_event("result_rejected", {"reason": "approved_answer_mismatch"})
                return "discard"
        with run_lock:
            candidate_events = list(event_log.events)
        candidate = AppResult(
            status=str(proposal.get("status") or "failed"),
            user_response=str(proposal.get("text") or ""),
            request_id=contract.request_id,
            contract_id=contract.contract_id,
            run_id=run_id,
            events=candidate_events,
            decision_trace=contract.decision_trace,
            control_request=(
                dict(proposal["control_request"])
                if isinstance(proposal.get("control_request"), dict)
                else None
            ),
            ok=True,
        )
        try:
            if (
                candidate.status == "answered"
                and approved_answer_text is None
                and contract.execution_environment != "eval"
            ):
                rejection_reason = "answer_not_approved"
                admitted_result = replace(
                    candidate,
                    status="failed",
                    user_response="Copilot 结果未通过结构或安全校验。",
                    error={"code": "RESULT_REJECTED", "reason": rejection_reason},
                    ok=False,
                )
            else:
                admitted = admit_result_with_decision(candidate)
                rejection_reason = admitted.rejection_reason
                admitted_result = admitted.result
            desired = "discard" if rejection_reason else "commit"
            if host_store is not None:
                winner = host_store.claim_admission_decision(run_id, desired)
            else:
                with run_lock:
                    if local_admission_state == "open":
                        local_admission_state = desired
                    winner = local_admission_state
            if winner == "cancel":
                retained_result = None
                retained_decision = "cancel"
                return "cancel"
            if winner not in {"commit", "discard"}:
                raise RuntimeError("invalid admission winner")
            retained_decision = winner
            retained_result = admitted_result if winner == desired else _failed_result(
                contract,
                run_id,
                event_log,
                code="INTERNAL_ERROR",
                response="Copilot 结果准入状态不一致。",
            )
            if rejection_reason:
                record_event("result_rejected", {"reason": rejection_reason})
            return winner
        except Exception:
            failed = _failed_result(
                contract,
                run_id,
                event_log,
                code="INTERNAL_ERROR",
                response="Copilot 结果准入失败。",
            )
            try:
                if host_store is not None:
                    winner = host_store.claim_admission_decision(run_id, "discard")
                else:
                    with run_lock:
                        if local_admission_state == "open":
                            local_admission_state = "discard"
                        winner = local_admission_state
            except Exception:
                winner = "discard"
            retained_result = None if winner == "cancel" else failed
            retained_decision = winner
            return winner if winner in {"commit", "discard", "cancel"} else "discard"

    try:
        process_result = run_pi_agent(
            start_payload,
            request_id=contract.request_id,
            run_id=run_id,
            timeout_seconds=limits["timeout_seconds"],
            on_event=on_process_event,
            on_tool_call=on_tool_call,
            on_proposed=on_proposed,
            is_cancelled=cancellation_requested,
            environ=process_environ,
        )
    except Exception:
        process_result = {
            "ok": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "stage": "runtime",
                "message": "Pi process adapter failed",
                "retryable": False,
            },
        }
    finally:
        with run_lock:
            tool_events_open = False

    if process_result.get("ok") is True:
        final = dict(process_result.get("result") or {})
        if final.get("status") == "cancelled":
            if retained_decision not in {"commit", "discard"}:
                record_terminal_event("run_cancelled", {"reason": "cancellation_requested"})
                return finish(
                    AppResult(
                        status="cancelled",
                        user_response="Copilot 运行已取消。",
                        error={"code": "CANCELLED"},
                        request_id=contract.request_id,
                        contract_id=contract.contract_id,
                        run_id=run_id,
                        events=list(event_log.events),
                        decision_trace=contract.decision_trace,
                        ok=False,
                    )
                )
            process_result = {
                "ok": False,
                "error": {
                    "code": "PROTOCOL_ERROR",
                    "stage": "protocol",
                    "message": "cancelled final conflicts with admission decision",
                    "retryable": False,
                },
            }
        committed_matches = final.get("committed") is (retained_decision == "commit")
        if (
            process_result.get("ok") is True
            and retained_result is not None
            and retained_decision in {"commit", "discard"}
            and committed_matches
        ):
            return finish(retained_result)
        process_result = {
            "ok": False,
            "error": {
                "code": "PROTOCOL_ERROR",
                "stage": "protocol",
                "message": "missing retained admission result",
                "retryable": False,
            },
        }

    error = dict(process_result.get("error") or {})
    source_code = str(error.get("code") or "INTERNAL_ERROR")
    if source_code == "MODEL_ERROR" and last_answer_rejection_reason:
        source_code = "ANSWER_ADMISSION_FAILED"
        error = {
            "code": source_code,
            "stage": "answer",
            "message": "answer admission failed after a rejected submit_answer",
            "retryable": False,
        }
    admission_winner = close_host_admission(
        "cancelled" if source_code == "CANCELLED" else "failed"
    )
    if admission_winner == "cancel":
        source_code = "CANCELLED"
        error = {
            "code": "CANCELLED",
            "stage": "cancel",
            "message": "cancellation requested",
            "retryable": False,
        }
    unknown_commit = retained_decision == "commit" or admission_winner == "commit"
    private_error = {
        "source_code": source_code,
        "stage": str(error.get("stage") or "runtime"),
        "retryable": bool(error.get("retryable")),
        **({"session_commit_outcome": "unknown"} if unknown_commit else {}),
    }
    if source_code == "CANCELLED":
        record_terminal_event("run_cancelled", private_error)
    elif source_code == "ANSWER_ADMISSION_FAILED":
        record_terminal_event("answer_admission_failed", private_error)
    else:
        record_terminal_event("model_error", private_error)
    return finish(
        _process_error_result(
            contract,
            run_id,
            event_log,
            error,
            unknown_commit=unknown_commit,
        )
    )


def _manifest_with_tool_descriptions(
    manifest: SceneManifest,
    *,
    control_preview_specs: tuple[dict[str, Any], ...] = (),
) -> SceneManifest:
    # Scene preparation already froze the model-visible business projection.
    # Reusing it here keeps the activation schema and catalog hash on one
    # canonical projection instead of re-reading the live registry.
    descriptions = [
        dict(item)
        for item in manifest.tool_descriptions
        if str(item.get("name") or "") in set(manifest.allowed_tools)
    ]
    if manifest.tool_loading_mode == "directory":
        descriptions = [
            _tool_directory_description(),
            _submit_answer_description(),
        ]
    else:
        descriptions.append(_submit_answer_description())
    if control_preview_specs:
        descriptions.append(control_preview_tool_description(control_preview_specs))
    provider_tools = [
        {
            "name": str(item.get("name") or ""),
            "description": str(item.get("description") or ""),
            "input_schema": dict(item.get("input_schema") or {}),
        }
        for item in descriptions
    ]
    canonical_tools = json.dumps(
        provider_tools,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return replace(
        manifest,
        tool_descriptions=descriptions,
        provenance={
            **manifest.provenance,
            "tool_schema_sha256": hashlib.sha256(canonical_tools.encode("utf-8")).hexdigest(),
            "tool_count": len(provider_tools),
        },
    )


def _scene_prepared_payload(manifest: SceneManifest) -> dict[str, Any]:
    provenance = manifest.provenance
    catalog_json = json.dumps(
        manifest.tool_catalog,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "scene": manifest.scene_name,
        "scene_version": manifest.scene_version,
        "fragments": [dict(item) for item in provenance.get("fragments") or ()],
        "compiled_prompt_sha256": str(provenance.get("compiled_prompt_sha256") or ""),
        "selected_toolsets": list(manifest.selected_toolsets),
        "tool_count": int(provenance.get("tool_count") or 0),
        "tool_schema_sha256": str(provenance.get("tool_schema_sha256") or ""),
        "tool_loading_mode": manifest.tool_loading_mode,
        "catalog_hash": manifest.catalog_hash,
        "catalog_entry_count": len(manifest.tool_catalog),
        "catalog_characters": len(catalog_json),
        "catalog_token_estimate": copilot_tools.conservative_json_tokens(
            manifest.tool_catalog
        ),
    }


def _manifest_prompt_parts(
    manifest: SceneManifest,
    contract: ExecutionContract,
) -> tuple[str, list[dict[str, str]], str]:
    messages = manifest.messages
    expected_user = str(contract.input.get("user_message") or "")
    if len(messages) < 2 or any(
        not isinstance(item, dict) or set(item) != {"role", "content"}
        for item in messages
    ):
        raise ValueError("Scene messages are not closed")
    if (
        messages[0].get("role") != "system"
        or not str(messages[0].get("content") or "")
        or messages[-1] != {"role": "user", "content": expected_user}
        or any(
            item.get("role") != "system" or not str(item.get("content") or "")
            for item in messages[1:-1]
        )
    ):
        raise ValueError("Scene message order is invalid")
    return (
        str(messages[0]["content"]),
        [
            {"role": "system", "content": str(item["content"])}
            for item in messages[1:-1]
        ],
        expected_user,
    )


def _provider_tools(manifest: SceneManifest) -> list[dict[str, Any]]:
    return [
        {
            "name": str(item.get("name") or ""),
            "description": str(item.get("description") or ""),
            "input_schema": deepcopy(dict(item.get("input_schema") or {})),
        }
        for item in manifest.tool_descriptions
    ]


def _tool_directory_description() -> dict[str, Any]:
    return {
        "name": "tool_directory",
        "description": "激活当前请求所需的授权业务工具集合。只能选择目录中名称，最多两个工具集和六个工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "catalog_hash": {"type": "string"},
                "tool_names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 6},
            },
            "required": ["catalog_hash", "tool_names"],
            "additionalProperties": False,
        },
        "catalog_summary": "激活授权业务工具",
    }


def _submit_answer_description() -> dict[str, Any]:
    return {
        "name": "submit_answer",
        "description": (
            "提交经过证据范围和新鲜度校验的结构化最终答案。"
            "observation_ids 只能引用本次 request 内成功读取的证据，不得复用历史会话 observation。"
            "claim kind 必须匹配证据 freshness：current/fresh + as_of 使用 current_fact；"
            "historical + as_of 使用 historical_fact 或 derived_fact；"
            "unknown/stale 只能在不完整答案中使用 judgment。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["conceptual", "evidence"]},
                "status": {"type": "string", "enum": ["complete", "partial", "needs_narrowing", "insufficient_evidence"]},
                "answer_markdown": {"type": "string", "minLength": 1, "maxLength": 12000},
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "minLength": 1},
                            "kind": {"type": "string", "enum": ["current_fact", "historical_fact", "derived_fact", "judgment"]},
                            "observation_ids": {"type": "array", "items": {"type": "string"}},
                            "required_scope": {"type": "string", "enum": ["point", "requested_page", "full_query"]},
                        },
                        "required": ["text", "kind", "observation_ids", "required_scope"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["mode", "status", "answer_markdown", "claims"],
            "additionalProperties": False,
        },
    }


def _process_limits(
    manifest: SceneManifest,
) -> dict[str, int]:
    values = manifest.limits
    return {
        "timeout_seconds": max(1, int(values.get("timeout_seconds") or 1)),
        "max_iterations": max(1, int(values.get("max_model_turns") or 1)),
        "max_tool_calls": max(1, int(values.get("max_tool_calls") or 1)),
        "max_consecutive_failed_tool_batches": max(
            1,
            int(values.get("max_consecutive_failed_tool_batches") or 1),
        ),
        "final_answer_reserve_seconds": max(
            1,
            int(values.get("final_answer_reserve_seconds") or 1),
        ),
    }


def _effective_model_payload(
    settings: PiModelSettings,
    scene_timeout_seconds: int,
) -> dict[str, Any]:
    payload = settings.process_payload()
    payload["timeout_seconds"] = min(
        settings.timeout_seconds,
        scene_timeout_seconds,
    )
    return payload


def _pi_session_id(contract: ExecutionContract, session_key: str | None) -> str | None:
    if contract.execution_environment == "eval":
        return None
    config_key = str(contract.input.get("config_key") or "").strip().lower()
    config_path = str(contract.input.get("config_path") or "").strip()
    authority_scope = f"key:{config_key or 'default'}"
    if contract.execution_environment != "channel":
        if not str(session_key or "").strip():
            return None
        return derive_pi_local_session_id(authority_scope, str(session_key).strip())
    channel = str(contract.input.get("authenticated_channel") or "").strip().lower()
    sender = str(contract.input.get("authenticated_sender_id") or "").strip()
    conversation = str(contract.input.get("authenticated_conversation_id") or "").strip()
    if not channel or not sender or bool(config_key) == bool(config_path):
        raise ValueError("channel Session identity is incomplete")
    if config_path:
        resolved_path = resolve_runtime_config_path(config_path=config_path).resolve(strict=True)
        if not resolved_path.is_file():
            raise ValueError("channel config_path must resolve to a regular file")
        authority_scope = "path:" + hashlib.sha256(
            str(resolved_path).encode("utf-8")
        ).hexdigest()
    else:
        resolve_runtime_config_path(config_key=config_key)
    expected = derive_pi_session_id(
        channel,
        sender,
        conversation or f"sender:{sender}",
        authority_scope,
    )
    if session_key != expected:
        raise ValueError("channel lease and Pi Session identity differ")
    return expected


def _bounded_recovered_observations(
    observations: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    bounded: list[dict[str, Any]] = []
    total_chars = 0
    for raw in reversed(observations):
        if not isinstance(raw, dict) or raw.get("ok") is not True:
            continue
        item = redact_value(dict(raw))
        encoded = json.dumps(item, ensure_ascii=False, default=str)
        if len(encoded) > 12_000:
            item = {
                key: item[key]
                for key in ("tool_name", "ok", "status", "summary", "source", "ref")
                if key in item
            }
            item["truncated"] = True
            encoded = json.dumps(item, ensure_ascii=False, default=str)
        if total_chars + len(encoded) > 48_000:
            continue
        bounded.append(item)
        total_chars += len(encoded)
        if len(bounded) == 8:
            break
    bounded.reverse()
    return bounded


def _observation_content_hash(observation: dict[str, Any]) -> str:
    hash_projection = {
        key: observation[key]
        for key in (
            "tool_name",
            "ok",
            "status",
            "summary",
            "value",
            "source",
            "scope",
            "coverage",
            "freshness",
            "as_of",
            "missing_data",
            "warnings",
            "result_contract",
        )
        if key in observation
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(
            hash_projection,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _tool_error(tool_name: str, code: str, message: str) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "ok": False,
        "status": "failed",
        "error": code,
        "code": code,
        "message": message,
        "retryable": False,
    }


def _failed_result(
    contract: ExecutionContract,
    run_id: str,
    event_log: CopilotEventLog,
    *,
    code: str,
    response: str,
) -> AppResult:
    return AppResult(
        status="failed",
        user_response=response,
        error={"code": code},
        request_id=contract.request_id,
        contract_id=contract.contract_id,
        run_id=run_id,
        events=list(event_log.events),
        decision_trace=contract.decision_trace,
        ok=False,
    )


def _process_error_result(
    contract: ExecutionContract,
    run_id: str,
    event_log: CopilotEventLog,
    error: dict[str, Any],
    *,
    unknown_commit: bool,
) -> AppResult:
    source_code = str(error.get("code") or "INTERNAL_ERROR")
    public_code = _PROCESS_ERROR_CODES.get(source_code, "INTERNAL_ERROR")
    retryable_session = source_code == "SESSION_ERROR" and bool(error.get("retryable"))
    response = {
        "CANCELLED": "Copilot 运行已取消。",
        "CONFIG_ERROR": "Copilot 运行配置无效。",
        "DEPENDENCY_MISSING": "Copilot 运行组件不可用。",
        "MODEL_ERROR": "Copilot 模型暂时不可用。",
        "TOOL_ERROR": "Copilot 读取数据失败。",
        "BUDGET_EXHAUSTED": "Copilot 未能在本次运行预算内完成回答。",
        "ANSWER_ADMISSION_FAILED": "Copilot 已读取数据，但答案未通过证据校验。",
        "INTERNAL_ERROR": (
            "会话暂时繁忙，请稍后重试。"
            if retryable_session
            else "Copilot 运行失败。"
        ),
    }[public_code]
    trace = {
        **contract.decision_trace,
        "pi_process": {
            "code": source_code,
            "stage": str(error.get("stage") or "runtime"),
            "retryable": bool(error.get("retryable")),
            **({"session_commit_outcome": "unknown"} if unknown_commit else {}),
        },
    }
    return AppResult(
        status="cancelled" if public_code == "CANCELLED" else "failed",
        user_response=response,
        error={"code": public_code},
        request_id=contract.request_id,
        contract_id=contract.contract_id,
        run_id=run_id,
        events=list(event_log.events),
        decision_trace=trace,
        ok=False,
    )


def _contract_rejection_reason(contract: ExecutionContract) -> str | None:
    if contract.policy.get("read_only") is not True:
        return "read_only_required"
    return scene_policy_rejection_reason(contract)


def _fixture_id(contract: ExecutionContract) -> str | None:
    value = contract.input.get("fixture_id")
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _default_fixture_observations(fixture_id: str | None) -> list[dict[str, Any]]:
    provider = importlib.import_module("src.application.copilot.eval_fixtures")
    return provider.fixture_observations(fixture_id)


__all__ = ["host_lane_slot", "run_contract", "session_run_slot"]
