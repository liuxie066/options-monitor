from __future__ import annotations

import importlib
from contextlib import contextmanager
from dataclasses import replace
from threading import Lock
from typing import Any, Callable

from src.application.copilot import tools as copilot_tools
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
) -> None:
    if host_store is not None:
        host_store.record_session_turn(
            session_key,
            user_message,
            assistant_message,
            max_messages=conversation_max_messages(),
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


def run_contract(
    contract: ExecutionContract,
    *,
    model_runner: ModelRunner | None = None,
    is_cancelled: CancellationChecker | None = None,
    fixture_observations_loader: FixtureObservationLoader | None = None,
    host_store: CopilotHostStore | None = None,
    session_key: str | None = None,
) -> AppResult:
    run_id = new_id("run")
    if host_store is not None:
        host_store.start_run(
            run_id,
            request_id=contract.request_id,
            contract_id=contract.contract_id,
            session_key=session_key,
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
        manifest = _manifest_with_tool_descriptions(build_scene_manifest(contract, run_id))
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

    fixture_id = _fixture_id(contract)
    engine_result = run_engine(
        manifest,
        scene_input=contract.input,
        record_event=event_log.record,
        build_tool_payload=lambda name, payload: copilot_tools.build_tool_payload(
            name,
            payload,
            static_payloads=manifest.tool_static_payloads,
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
    )
    result = AppResult(
        status=engine_result.status,
        user_response=engine_result.text,
        error=engine_result.error,
        request_id=contract.request_id,
        contract_id=contract.contract_id,
        run_id=run_id,
        events=event_log.events,
        decision_trace=contract.decision_trace,
        ok=engine_result.status not in {"failed", "cancelled"},
    )
    admitted = admit_result_with_decision(result)
    if admitted.rejection_reason:
        event_log.record("result_rejected", {"reason": admitted.rejection_reason})
    return finish(admitted.result)


def _manifest_with_tool_descriptions(manifest: SceneManifest) -> SceneManifest:
    return replace(
        manifest,
        tool_descriptions=copilot_tools.tool_descriptions(
            manifest.allowed_tools,
            static_payloads=manifest.tool_static_payloads,
        ),
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


__all__ = ["record_session_turn", "run_contract", "session_messages", "session_run_slot"]
