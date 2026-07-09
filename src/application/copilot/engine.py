from __future__ import annotations

import time
from typing import Any, Callable

from src.application.copilot.agent import (
    ActionDecider,
    AgentAction,
    AgentState,
    default_action_decider,
)
from src.application.copilot.contracts import SceneManifest, new_id


ToolPayloadBuilder = Callable[[str, dict[str, Any]], tuple[dict[str, Any] | None, str | None]]
ReadToolCaller = Callable[[str, dict[str, Any]], dict[str, Any]]
ObservationCompactor = Callable[[str, dict[str, Any]], dict[str, Any]]
ObservationEventBuilder = Callable[[dict[str, Any], int], dict[str, Any]]
FixtureLoader = Callable[[str | None], list[dict[str, Any]]]
EventRecorder = Callable[[str, dict[str, Any], str | None], None]
Clock = Callable[[], float]
CancellationChecker = Callable[[], bool]
ObservationProjector = Callable[[list[dict[str, Any]], bool], Any]
AgentReportProjector = Callable[[list[dict[str, Any]], dict[str, Any], list[str]], Any]


def run_engine(
    manifest: SceneManifest,
    *,
    scene_input: dict[str, Any],
    record_event: EventRecorder,
    build_tool_payload: ToolPayloadBuilder,
    call_read_tool: ReadToolCaller,
    compact_observation: ObservationCompactor,
    build_observation_event: ObservationEventBuilder,
    fixture_observations: FixtureLoader,
    project_observations: ObservationProjector,
    project_agent_report: AgentReportProjector,
    use_mock_observations: bool = False,
    fixture_id: str | None = None,
    require_mock_model_synthesis: bool = False,
    decide_next_action: ActionDecider | None = None,
    clock: Clock | None = None,
    is_cancelled: CancellationChecker | None = None,
) -> Any:
    if use_mock_observations:
        try:
            fixture_items = list(fixture_observations(fixture_id))
        except Exception:
            fixture_items = [_tool_exception_response("fixture", code="FIXTURE_ERROR")]
        observations = []
        for index, item in enumerate(fixture_items, start=1):
            try:
                observation = build_observation_event(item, index)
            except Exception:
                observation = build_observation_event(_tool_exception_response("fixture", code="FIXTURE_ERROR"), index)
            observations.append(observation)
        for item in observations:
            record_event("observation", item, item["ref"])
        if decide_next_action is not None:
            return _run_mock_model_synthesis(
                manifest,
                observations=observations,
                record_event=record_event,
                build_observation_event=build_observation_event,
                project_observations=project_observations,
                project_agent_report=project_agent_report,
                decide_next_action=decide_next_action,
            )
        if require_mock_model_synthesis:
            state = AgentState(
                manifest=manifest,
                observations=list(observations),
                attempted_tools=_attempted_allowed_tools(manifest, observations),
            )
            _append_recoverable_observation(
                state,
                record_event,
                build_observation_event,
                tool_name="eval_model",
                code="MODEL_REQUIRED",
                summary="Eval model synthesis is required for this fixture.",
                missing_data="eval model answer report",
            )
            return project_observations(state.observations, True)
        return project_observations(observations, True)

    state = AgentState(manifest=manifest)
    decider = decide_next_action or default_action_decider
    max_turns = int(manifest.limits.get("max_model_turns") or 0)
    max_tool_calls = int(manifest.limits.get("max_tool_calls") or 0)
    timeout_seconds = float(manifest.limits.get("timeout_seconds") or 0)
    clock_fn = clock or time.monotonic
    started_at = clock_fn()
    finished = False

    while state.turns < max_turns:
        if is_cancelled and is_cancelled():
            record_event("run_cancelled", {"reason": "cancellation_requested"}, None)
            _append_recoverable_observation(
                state,
                record_event,
                build_observation_event,
                tool_name="cancellation",
                code="CANCELLED",
                summary="Run was cancelled before completion.",
                missing_data="remaining agent/tool work",
            )
            break
        if timeout_seconds > 0 and clock_fn() - started_at >= timeout_seconds:
            record_event("budget_exhausted", {"limit": "timeout_seconds"}, None)
            _append_recoverable_observation(
                state,
                record_event,
                build_observation_event,
                tool_name="timeout",
                code="BUDGET_EXHAUSTED",
                summary="Run timeout expired before completing remaining work.",
                missing_data="remaining agent/tool work",
            )
            break

        try:
            action = decider(state)
        except Exception:
            action = AgentAction(kind="invalid", reason="action_decider_exception")
        state.turns += 1
        record_event("agent_action", _agent_action_payload(state.turns, action, manifest.allowed_tools), None)
        if action.kind in {"tool", "finish"}:
            _record_model_error_if_present(state.turns, action, record_event)

        if action.kind == "finish":
            if isinstance(action.final_report, dict):
                return project_agent_report(
                    state.observations,
                    action.final_report,
                    manifest.allowed_tools,
                )
            if _has_unattempted_manifest_tools(state):
                record_event(
                    "agent_action_rejected",
                    {"turn": state.turns, "reason": "early_finish_without_report"},
                    None,
                )
                _append_recoverable_observation(
                    state,
                    record_event,
                    build_observation_event,
                    tool_name="agent_finish",
                    code="INSUFFICIENT_EVIDENCE",
                    summary="Agent finished without an answer report before remaining work completed.",
                    missing_data="remaining agent/tool work",
                )
                finished = True
                break
            finished = True
            break
        if action.kind != "tool" or not action.tool_name:
            _record_model_error_if_present(state.turns, action, record_event)
            record_event("agent_action_rejected", {"turn": state.turns, "reason": "invalid_action"}, None)
            _append_recoverable_observation(
                state,
                record_event,
                build_observation_event,
                tool_name="agent_action",
                code=_action_error_code(action) or "INVALID_ACTION",
                summary="Next action was invalid; no tool was executed.",
                missing_data=_invalid_action_missing_data(action),
            )
            continue
        if action.tool_name not in manifest.allowed_tools:
            record_event(
                "agent_action_rejected",
                {"turn": state.turns, "reason": "tool_not_allowed", "tool_name_allowed": False},
                None,
            )
            _append_recoverable_observation(
                state,
                record_event,
                build_observation_event,
                tool_name="agent_action",
                code="POLICY_ERROR",
                summary="Tool call was rejected by execution policy.",
                missing_data="allowed tool observation",
            )
            continue
        if action.tool_name in set(state.attempted_tools):
            record_event(
                "agent_action_rejected",
                {"turn": state.turns, "reason": "tool_already_attempted"},
                None,
            )
            _append_recoverable_observation(
                state,
                record_event,
                build_observation_event,
                tool_name="agent_action",
                code="POLICY_ERROR",
                summary="Tool call repeated an already attempted tool; no tool was executed.",
                missing_data="valid agent action",
            )
            continue
        if state.tool_calls >= max_tool_calls:
            record_event("budget_exhausted", {"limit": "max_tool_calls"}, None)
            _append_recoverable_observation(
                state,
                record_event,
                build_observation_event,
                tool_name=action.tool_name,
                code="BUDGET_EXHAUSTED",
                summary="Tool call budget was exhausted before remaining tools completed.",
                missing_data="remaining tool observations",
            )
            break

        state.attempted_tools.append(action.tool_name)
        try:
            payload, skip_reason = build_tool_payload(action.tool_name, scene_input)
        except Exception:
            payload, skip_reason = None, "payload_exception"
        if skip_reason:
            record_event("tool_skipped", {"tool_name": action.tool_name, "reason": "payload_unavailable"}, None)
            _append_recoverable_observation(
                state,
                record_event,
                build_observation_event,
                tool_name=action.tool_name,
                code="INPUT_ERROR",
                summary="Required tool input was unavailable.",
                missing_data=f"{action.tool_name} required input",
            )
            continue
        state.tool_calls += 1
        tool_call_id = new_id("toolcall")
        record_event("tool_attempt", _tool_attempt_payload(action.tool_name, payload, tool_call_id, state.turns), None)
        try:
            response = call_read_tool(action.tool_name, payload or {})
        except Exception:
            response = _tool_exception_response(action.tool_name)
        try:
            observation = build_observation_event(
                compact_observation(action.tool_name, response),
                len(state.observations) + 1,
            )
        except Exception:
            observation = build_observation_event(
                _tool_exception_response(action.tool_name, code="OBSERVATION_ERROR"),
                len(state.observations) + 1,
            )
        observation["tool_call_id"] = tool_call_id
        state.observations.append(observation)
        record_event("observation", observation, observation["ref"])
        if not bool(observation.get("ok")):
            record_event(
                "tool_failed",
                {
                    "tool_name": action.tool_name,
                    "tool_call_id": tool_call_id,
                    "error_code": _error_code(observation),
                },
                None,
            )

    if not finished and state.turns >= max_turns and _has_unattempted_manifest_tools(state):
        record_event("budget_exhausted", {"limit": "max_model_turns"}, None)
        _append_recoverable_observation(
            state,
            record_event,
            build_observation_event,
            tool_name="agent_turns",
            code="BUDGET_EXHAUSTED",
            summary="Action turn budget was exhausted before remaining work completed.",
            missing_data="remaining agent/tool work",
        )

    return project_observations(state.observations, False)


def _run_mock_model_synthesis(
    manifest: SceneManifest,
    *,
    observations: list[dict[str, Any]],
    record_event: EventRecorder,
    build_observation_event: ObservationEventBuilder,
    project_observations: ObservationProjector,
    project_agent_report: AgentReportProjector,
    decide_next_action: ActionDecider,
) -> Any:
    state = AgentState(
        manifest=manifest,
        observations=list(observations),
        attempted_tools=_attempted_allowed_tools(manifest, observations),
    )
    max_turns = int(manifest.limits.get("max_model_turns") or 0)
    while state.turns < max_turns:
        try:
            action = decide_next_action(state)
        except Exception:
            action = AgentAction(kind="invalid", reason="action_decider_exception")
        state.turns += 1
        record_event("agent_action", _agent_action_payload(state.turns, action, manifest.allowed_tools), None)
        _record_model_error_if_present(state.turns, action, record_event)
        if _action_error_code(action) == "MODEL_ERROR":
            _append_recoverable_observation(
                state,
                record_event,
                build_observation_event,
                tool_name="eval_model",
                code="MODEL_ERROR",
                summary="Eval model synthesis was unavailable.",
                missing_data="eval model answer report",
            )
            break
        if _action_error_code(action) == "MODEL_ACTION_INVALID":
            _append_recoverable_observation(
                state,
                record_event,
                build_observation_event,
                tool_name="eval_model",
                code="MODEL_ACTION_INVALID",
                summary="Eval model action was invalid.",
                missing_data=_invalid_action_missing_data(action),
            )
            break
        if action.kind == "finish" and isinstance(action.final_report, dict):
            return project_agent_report(
                state.observations,
                action.final_report,
                manifest.allowed_tools,
            )
        record_event(
            "agent_action_rejected",
            {"turn": state.turns, "reason": "eval_fixture_requires_finish"},
            None,
        )
        _append_recoverable_observation(
            state,
            record_event,
            build_observation_event,
            tool_name="eval_fixture",
            code="POLICY_ERROR",
            summary="Eval fixture observations were already loaded; model must finish instead of requesting tools.",
            missing_data="eval fixture answer report",
        )
    return project_observations(state.observations, True)


def _attempted_allowed_tools(manifest: SceneManifest, observations: list[dict[str, Any]]) -> list[str]:
    allowed = set(manifest.allowed_tools)
    return [
        tool_name
        for tool_name in (str(item.get("tool_name") or "").strip() for item in observations)
        if tool_name in allowed
    ]


def _has_unattempted_manifest_tools(state: AgentState) -> bool:
    attempted = set(state.attempted_tools)
    return any(tool_name not in attempted for tool_name in state.manifest.allowed_tools)


def _tool_attempt_payload(
    tool_name: str,
    payload: dict[str, Any] | None,
    tool_call_id: str,
    turn: int,
) -> dict[str, Any]:
    payload_dict = payload if isinstance(payload, dict) else {}
    return {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "turn": turn,
        "payload_keys": sorted(key for key in payload_dict if isinstance(key, str)),
    }


def _tool_exception_response(tool_name: str, *, code: str = "TOOL_EXCEPTION") -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "ok": False,
        "summary": f"{tool_name} failed with code {code}.",
        "error": {"code": code},
        "evidence_ok": False,
        "missing_data": [f"{tool_name} evidence unavailable: {code}"],
    }


def _agent_action_payload(turn: int, action: Any, allowed_tools: list[str]) -> dict[str, Any]:
    payload = {"turn": turn, "kind": action.kind}
    if action.tool_name and action.tool_name in allowed_tools:
        payload["tool_name"] = action.tool_name
    elif action.tool_name:
        payload["tool_name_allowed"] = False
    if action.reason:
        payload["reason_present"] = True
    if action.kind == "finish" and isinstance(getattr(action, "final_report", None), dict):
        payload["final_report_present"] = True
    return payload


def _record_model_error_if_present(turn: int, action: Any, record_event: EventRecorder) -> None:
    code = _action_error_code(action)
    if code not in {"MODEL_ERROR", "MODEL_ACTION_INVALID"}:
        return
    record_event("model_error", {"turn": turn, "code": code}, None)


def _action_error_code(action: Any) -> str | None:
    code = str(getattr(action, "error_code", "") or "").strip().upper()
    return code or None


def _invalid_action_missing_data(action: Any) -> str:
    reason = str(getattr(action, "reason", "") or "")
    if reason.strip().endswith("finish action requires conclusion"):
        return "valid conclusion"
    if "finish action requires cited findings" in reason:
        return "cited findings"
    if "finish action requires finding evidence" in reason:
        return "finding evidence"
    if "finish action requires recommendations empty when evidence missing" in reason:
        return "recommendations blocked by missing evidence"
    if "finish action requires concrete recommendations" in reason:
        return "concrete recommendations"
    if "finish action requires substantive conclusion" in reason:
        return "substantive conclusion"
    if "finish action requires conclusion finding support" in reason:
        return "conclusion finding support"
    if "finish action requires conclusion missing-data support" in reason:
        return "conclusion missing-data support"
    if "finish action requires conclusion target support" in reason:
        return "conclusion target support"
    if "finish action requires conclusion account support" in reason:
        return "conclusion account support"
    if "finish action requires conclusion numeric support" in reason:
        return "conclusion numeric support"
    if "finish action violates evidence-use boundary" in reason:
        return "evidence-use boundary"
    if "finish action violates omitted-evidence boundary" in reason:
        return "omitted evidence boundary"
    if "finish action requires recommendations" in reason:
        return "cited recommendations"
    if "finish action requires recommendation evidence" in reason:
        return "recommendation evidence"
    if "finish action requires recommendation finding support" in reason:
        return "recommendation finding support"
    if "finish action requires substantive findings" in reason:
        return "substantive findings"
    if "finish action requires recommendation answer dimension" in reason:
        return "recommendation answer dimension"
    if "finish action requires answer-dimension findings" in reason:
        return "answer-dimension findings"
    if "finish action requires finding dimension support" in reason:
        return "finding dimension support"
    if "finish action requires recommendation dimension evidence" in reason:
        return "recommendation dimension evidence"
    if "finish action requires recommendation dimension support" in reason:
        return "recommendation dimension support"
    if "finish action requires recommendation target support" in reason:
        return "recommendation target support"
    if "finish action requires recommendation account support" in reason:
        return "recommendation account support"
    if "finish action requires recommendation numeric support" in reason:
        return "recommendation numeric support"
    if "finish action requires requested-period finding evidence" in reason:
        return "requested-period finding evidence"
    if "finish action requires requested-period recommendation evidence" in reason:
        return "requested-period recommendation evidence"
    if "finish action requires synthesized summaries" in reason:
        return "synthesized answer report"
    if "finish action requires tool evidence" in reason:
        return "remaining agent/tool work"
    if "finish action requires missing evidence" in reason:
        return "missing tool evidence"
    if "finish action requires eval fixture disclosure" in reason:
        return "eval fixture disclosure"
    if "finish action claims external action" in reason:
        return "mutation_claim"
    if "finish action uses non-claimable evidence refs" in reason:
        return "non-claimable evidence refs"
    return "valid agent action"


def _error_code(observation: dict[str, Any]) -> str | None:
    error = observation.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        return code or None
    return None


def _append_recoverable_observation(
    state: AgentState,
    record_event: EventRecorder,
    build_observation_event: ObservationEventBuilder,
    *,
    tool_name: str,
    code: str,
    summary: str,
    missing_data: str,
) -> None:
    observation = build_observation_event(
        {
            "tool_name": tool_name,
            "ok": False,
            "summary": summary,
            "data": {},
            "error": {"code": code, "message": summary},
            "evidence_ok": False,
            "missing_data": [missing_data],
        },
        len(state.observations) + 1,
    )
    state.observations.append(observation)
    record_event("observation", observation, observation["ref"])
