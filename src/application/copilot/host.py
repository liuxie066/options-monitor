from __future__ import annotations

import hashlib
import importlib
import json
from contextlib import contextmanager
from dataclasses import replace
from threading import Lock
from typing import Any, Callable

from src.application.copilot import tools as copilot_tools
from src.application.copilot.control_handoff import (
    CONTROL_PREVIEW_TOOL,
    build_control_preview_request,
    control_preview_tool_description,
)
from src.application.copilot.agent import ModelRunner
from src.application.copilot.contracts import AppResult, ExecutionContract, SceneManifest, new_id
from src.application.copilot.engine import run_engine
from src.application.copilot.event_store import CopilotEventLog
from src.application.copilot.host_store import CopilotHostStore
from src.application.copilot.result_admission import admit_result_with_decision
from src.application.copilot.scene import build_scene_manifest, conversation_max_messages, scene_policy_rejection_reason


CancellationChecker = Callable[[], bool]
FixtureObservationLoader = Callable[[str | None], list[dict[str, Any]]]
_SESSION_LOCK = Lock()
_RUNNING_SESSIONS: set[str] = set()
_SESSION_MESSAGES: dict[str, list[dict[str, str]]] = {}


def session_messages(
    session_key: str,
    *,
    host_store: CopilotHostStore | None = None,
) -> tuple[dict[str, str], ...]:
    if host_store is not None:
        return tuple(dict(item) for item in host_store.session_messages(session_key))
    with _SESSION_LOCK:
        return tuple(dict(item) for item in _SESSION_MESSAGES.get(session_key, ()))


def record_session_turn(
    session_key: str,
    user_message: str,
    assistant_message: str,
    *,
    host_store: CopilotHostStore | None = None,
    tool_uses: tuple[dict[str, Any], ...] = (),
    warnings: tuple[str, ...] = (),
    errors: tuple[str, ...] = (),
) -> None:
    if host_store is not None:
        host_store.record_session_turn(
            session_key,
            user_message,
            assistant_message,
            max_messages=conversation_max_messages(),
            tool_uses=tool_uses,
            warnings=warnings,
            errors=errors,
        )
        return
    with _SESSION_LOCK:
        messages = _SESSION_MESSAGES.setdefault(session_key, [])
        messages.extend(
            (
                {"role": "user", "content": str(user_message)},
                {"role": "assistant", "content": str(assistant_message)},
            )
        )
        max_messages = conversation_max_messages()
        if max_messages and len(messages) > max_messages:
            del messages[:-max_messages]


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
    model_runner: ModelRunner | None = None,
    is_cancelled: CancellationChecker | None = None,
    fixture_observations_loader: FixtureObservationLoader | None = None,
    host_store: CopilotHostStore | None = None,
    session_key: str | None = None,
    control_preview_specs: tuple[dict[str, Any], ...] = (),
    resumed_from: str | None = None,
    recovered_observations: tuple[dict[str, Any], ...] = (),
    enabled_optional_toolsets: frozenset[str] = frozenset(),
) -> AppResult:
    run_id = new_id("run")
    if host_store is not None:
        host_store.start_run(
            run_id,
            contract=contract,
            session_key=session_key,
            resumed_from=resumed_from,
        )
    event_log = CopilotEventLog(run_id, sink=host_store.append_event if host_store is not None else None)
    finish = lambda result: _finalize(event_log, result, host_store=host_store)
    event_log.record(
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
            event_log,
            AppResult(
                status="not_ready",
                user_response="Copilot 未执行请求，因为执行合同未通过只读策略校验。",
                error={"code": "CONTRACT_REJECTED", "reason": rejection},
                request_id=contract.request_id,
                contract_id=contract.contract_id,
                run_id=run_id,
                events=event_log.events,
                decision_trace=contract.decision_trace,
                ok=False,
            ),
        )
    try:
        scene_manifest = (
            build_scene_manifest(
                contract,
                run_id,
                enabled_optional_toolsets=enabled_optional_toolsets,
            )
            if enabled_optional_toolsets
            else build_scene_manifest(contract, run_id)
        )
        manifest = _manifest_with_tool_descriptions(
            scene_manifest,
            control_preview_specs=control_preview_specs if contract.execution_environment == "channel" else (),
        )
    except Exception:
        event_log.record("scene_preparation_failed", {"reason": "manifest_error"})
        return finish(
            event_log,
            AppResult(
                status="failed",
                user_response="Copilot 未能准备只读执行场景。",
                error={"code": "SCENE_PREPARATION_FAILED"},
                request_id=contract.request_id,
                contract_id=contract.contract_id,
                run_id=run_id,
                events=event_log.events,
                decision_trace=contract.decision_trace,
                ok=False,
            ),
        )

    event_log.record("scene_prepared", _scene_prepared_payload(manifest))
    fixture_id = _fixture_id(contract)
    engine_result = run_engine(
        manifest,
        user_message=str(contract.input.get("user_message") or ""),
        record_event=event_log.record,
        build_tool_payload=lambda name, payload, fixed_input: copilot_tools.build_tool_payload(
            name,
            payload,
            static_payloads=manifest.tool_static_payloads,
            fixed_input=fixed_input,
        ),
        call_read_tool=lambda name, payload: copilot_tools.call_read_tool(
            name,
            payload,
            allowed_tools=tuple(manifest.allowed_tools),
        ),
        compact_observation=copilot_tools.compact_observation,
        fixture_observations=fixture_observations_loader or _default_fixture_observations,
        model_runner=model_runner,
        use_mock_observations=fixture_id is not None,
        fixture_id=fixture_id,
        is_cancelled=(lambda: bool((is_cancelled and is_cancelled()) or (host_store and host_store.is_cancel_requested(run_id)))),
        control_tool_name=CONTROL_PREVIEW_TOOL if control_preview_specs and contract.execution_environment == "channel" else None,
        build_control_request=lambda arguments, user_message: build_control_preview_request(
            arguments,
            user_message=user_message,
            specs=control_preview_specs,
        ),
        recovered_observations=recovered_observations,
    )
    result = AppResult(
        status=engine_result.status,
        user_response=(
            engine_result.text
            or ("Copilot 运行已取消。" if engine_result.status == "cancelled" else "")
        ),
        error=engine_result.error,
        request_id=contract.request_id,
        contract_id=contract.contract_id,
        run_id=run_id,
        events=event_log.events,
        decision_trace=contract.decision_trace,
        control_request=engine_result.control_request,
        ok=engine_result.status not in {"failed", "cancelled"},
    )
    admitted = admit_result_with_decision(result)
    if admitted.rejection_reason:
        event_log.record("result_rejected", {"reason": admitted.rejection_reason})
    return finish(admitted.result)


def _manifest_with_tool_descriptions(
    manifest: SceneManifest,
    *,
    control_preview_specs: tuple[dict[str, Any], ...] = (),
) -> SceneManifest:
    descriptions = copilot_tools.tool_descriptions(
        manifest.allowed_tools,
        static_payloads=manifest.tool_static_payloads,
    )
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
    return {
        "scene": manifest.scene_name,
        "scene_version": manifest.scene_version,
        "fragments": [dict(item) for item in provenance.get("fragments") or ()],
        "compiled_prompt_sha256": str(provenance.get("compiled_prompt_sha256") or ""),
        "selected_toolsets": list(manifest.selected_toolsets),
        "tool_count": int(provenance.get("tool_count") or 0),
        "tool_schema_sha256": str(provenance.get("tool_schema_sha256") or ""),
    }


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


def _finalize(
    event_log: CopilotEventLog,
    result: AppResult,
    *,
    host_store: CopilotHostStore | None = None,
) -> AppResult:
    event_log.record_final_result(result)
    finalized = replace(result, events=event_log.events)
    if host_store is not None:
        host_store.finish_run(finalized)
    return finalized


__all__ = ["host_lane_slot", "record_session_turn", "run_contract", "session_messages", "session_run_slot"]
