from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from src.application.copilot.agent import AgentRunResult, AgentState, ModelRequest, ModelRunner, ToolCall
from src.application.copilot.contracts import SceneManifest, new_id


ToolPayloadBuilder = Callable[
    [str, dict[str, Any], dict[str, Any]],
    tuple[dict[str, Any] | None, str | None],
]
ReadToolCaller = Callable[[str, dict[str, Any]], dict[str, Any]]
ObservationCompactor = Callable[[str, dict[str, Any], dict[str, Any] | None], dict[str, Any]]
FixtureLoader = Callable[[str | None], list[dict[str, Any]]]
EventRecorder = Callable[[str, dict[str, Any], str | None], None]
Clock = Callable[[], float]
CancellationChecker = Callable[[], bool]
ControlRequestBuilder = Callable[[dict[str, Any], str], tuple[dict[str, Any] | None, str | None]]

OBSERVATION_PAGE_CHARS = 12_000
DEFAULT_MAX_CONTEXT_CHARS = 96_000
DEFAULT_MAX_CONTEXT_TOKENS = 24_000
TRANSIENT_TOOL_ERRORS = frozenset(
    {"REQUEST_TIMEOUT", "TOOL_EXECUTION_TIMEOUT", "EXECUTION_ERROR", "TOOL_EXCEPTION"}
)


def run_engine(
    manifest: SceneManifest,
    *,
    scene_input: dict[str, Any],
    record_event: EventRecorder,
    build_tool_payload: ToolPayloadBuilder,
    call_read_tool: ReadToolCaller,
    compact_observation: ObservationCompactor,
    fixture_observations: FixtureLoader,
    model_runner: ModelRunner | None,
    use_mock_observations: bool = False,
    fixture_id: str | None = None,
    clock: Clock | None = None,
    is_cancelled: CancellationChecker | None = None,
    control_tool_name: str | None = None,
    build_control_request: ControlRequestBuilder | None = None,
    recovered_observations: tuple[dict[str, Any], ...] = (),
) -> AgentRunResult:
    if model_runner is None:
        return AgentRunResult(
            status="failed",
            error={"code": "MODEL_REQUIRED", "message": "Copilot model is not configured"},
        )
    state = AgentState(manifest=manifest, messages=[dict(item) for item in manifest.messages])
    _load_recovered_observations(state, recovered_observations, record_event)
    if use_mock_observations:
        _load_fixture_observations(state, fixture_id, fixture_observations, record_event)

    max_iterations = max(1, int(manifest.limits.get("max_model_turns") or 1))
    max_tool_calls = max(0, int(manifest.limits.get("max_tool_calls") or 0))
    max_context_chars = max(
        8_000,
        int(manifest.limits.get("max_context_chars") or DEFAULT_MAX_CONTEXT_CHARS),
    )
    max_context_tokens = max(
        2_000,
        int(manifest.limits.get("max_context_tokens") or DEFAULT_MAX_CONTEXT_TOKENS),
    )
    timeout_seconds = max(0.0, float(manifest.limits.get("timeout_seconds") or 0))
    final_answer_reserve_seconds = max(
        1.0,
        float(manifest.limits.get("final_answer_reserve_seconds") or 30),
    )
    max_failed_batches = max(1, int(manifest.limits.get("max_consecutive_failed_tool_batches") or 3))
    clock_fn = clock or time.monotonic
    started_at = clock_fn()
    consecutive_failed_batches = 0

    while state.iterations < max_iterations:
        stop = _stop_reason(
            state,
            max_tool_calls=max_tool_calls,
            timeout_seconds=timeout_seconds,
            reserve_seconds=final_answer_reserve_seconds,
            started_at=started_at,
            clock=clock_fn,
            is_cancelled=is_cancelled,
        )
        if stop:
            record_event(stop[0], stop[1], None)
            break
        turn = _call_model(
            state,
            model_runner,
            force_finish=False,
            max_context_chars=max_context_chars,
            max_context_tokens=max_context_tokens,
            timeout_seconds=_remaining_seconds(timeout_seconds, started_at, clock_fn),
            record_event=record_event,
            is_cancelled=is_cancelled,
        )
        if isinstance(turn, AgentRunResult):
            if state.observations:
                break
            return turn
        if turn.finish_reason == "content_filter":
            return AgentRunResult(
                status="failed",
                error={"code": "MODEL_ERROR", "message": "model response was blocked by content filtering"},
            )
        if turn.text and not turn.tool_calls:
            if turn.finish_reason == "length" and state.iterations < max_iterations:
                state.accumulated_text_parts.append(turn.text)
                state.continuation_count += 1
                state.messages.extend(
                    (
                        {"role": "assistant", "content": turn.text},
                        {
                            "role": "system",
                            "content": (
                                "The previous answer was truncated. Continue from exactly where it stopped, "
                                "without repeating earlier text or calling tools unless essential."
                            ),
                        },
                    )
                )
                record_event(
                    "model_continuation_requested",
                    {"continuation_count": state.continuation_count, "finish_reason": "length"},
                    None,
                )
                continue
            text = _joined_answer(state.accumulated_text_parts, turn.text)
            record_event(
                "agent_terminated",
                {
                    "reason": "final_answer",
                    "continuation_count": state.continuation_count,
                    "compaction_count": state.compaction_count,
                    "model_retry_count": state.model_retry_count,
                    "usage_total": dict(state.token_usage),
                },
                None,
            )
            return AgentRunResult(status="answered", text=text)
        if not turn.tool_calls:
            state.messages.append(
                {
                    "role": "system",
                    "content": "The previous model turn returned neither text nor tool calls. Answer or call a tool.",
                }
            )
            continue

        _append_assistant_tool_calls(state, turn.text, turn.tool_calls)
        control_calls = [call for call in turn.tool_calls if control_tool_name and call.name == control_tool_name]
        if control_calls:
            if len(control_calls) != 1 or len(turn.tool_calls) != 1 or build_control_request is None:
                for call in control_calls:
                    _append_tool_observation(
                        state,
                        call,
                        _error_observation(
                            call.name,
                            "INVALID_ACTION",
                            "control preview must be the only action in a model turn",
                        ),
                        record_event,
                    )
                consecutive_failed_batches += 1
                continue
            call = control_calls[0]
            control_request, control_error = build_control_request(
                dict(call.arguments),
                str(scene_input.get("user_message") or ""),
            )
            if control_error or control_request is None:
                _append_tool_observation(
                    state,
                    call,
                    _error_observation(call.name, "INVALID_ACTION", control_error or "invalid control preview request"),
                    record_event,
                )
                consecutive_failed_batches += 1
                continue
            record_event(
                "control_preview_requested",
                {"intent_name": control_request.get("intent_name")},
                None,
            )
            return AgentRunResult(status="control_requested", control_request=control_request)
        batch_ok = False
        for call in turn.tool_calls:
            if state.tool_calls >= max_tool_calls:
                observation = _append_tool_observation(
                    state,
                    call,
                    _error_observation(call.name, "BUDGET_EXHAUSTED", "tool-call budget is exhausted"),
                    record_event,
                )
                batch_ok = batch_ok or bool(observation.get("ok"))
                continue
            observation = _execute_tool_call(
                state,
                call,
                scene_input=scene_input,
                build_tool_payload=build_tool_payload,
                call_read_tool=call_read_tool,
                compact_observation=compact_observation,
                record_event=record_event,
                is_cancelled=is_cancelled,
            )
            batch_ok = batch_ok or bool(observation.get("ok"))
        consecutive_failed_batches = 0 if batch_ok else consecutive_failed_batches + 1
        if consecutive_failed_batches >= max_failed_batches:
            record_event(
                "tool_failure_fallback",
                {"consecutive_failed_batches": consecutive_failed_batches},
                None,
            )
            break

    if is_cancelled and is_cancelled():
        return AgentRunResult(status="cancelled", error={"code": "CANCELLED", "message": "run cancelled"})
    final_turn = _call_model(
        state,
        model_runner,
        force_finish=True,
        max_context_chars=max_context_chars,
        max_context_tokens=max_context_tokens,
        timeout_seconds=_remaining_seconds(timeout_seconds, started_at, clock_fn),
        record_event=record_event,
        is_cancelled=is_cancelled,
    )
    if isinstance(final_turn, AgentRunResult):
        return final_turn
    if final_turn.text:
        text = _joined_answer(state.accumulated_text_parts, final_turn.text)
        record_event(
            "agent_terminated",
            {
                "reason": "forced_final_answer",
                "finish_reason": final_turn.finish_reason,
                "continuation_count": state.continuation_count,
                "compaction_count": state.compaction_count,
                "model_retry_count": state.model_retry_count,
                "usage_total": dict(state.token_usage),
            },
            None,
        )
        return AgentRunResult(status="answered", text=text)
    return AgentRunResult(
        status="failed",
        error={"code": "EMPTY_FINAL_RESPONSE", "message": "model did not produce a final answer"},
    )


def _call_model(
    state: AgentState,
    model_runner: ModelRunner,
    *,
    force_finish: bool,
    max_context_chars: int,
    max_context_tokens: int,
    timeout_seconds: int | None,
    record_event: EventRecorder,
    is_cancelled: CancellationChecker | None,
):
    tools = () if force_finish else tuple(_model_tools(state))
    messages, omitted = _bounded_messages(
        state.messages,
        max_chars=max_context_chars,
        max_tokens=max_context_tokens,
    )
    iteration_id = new_id("iter")
    context_hash = hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    request = ModelRequest(
        messages=messages,
        tools=tools,
        force_finish=force_finish,
        timeout_seconds=timeout_seconds,
        is_cancelled=is_cancelled,
        iteration_id=iteration_id,
        context_hash=context_hash,
    )
    state.iterations += 1
    state.current_iteration_id = iteration_id
    if omitted:
        state.compaction_count += 1
        record_event(
            "context_compacted",
            {
                "iteration": state.iterations,
                "omitted_message_count": omitted,
                "max_context_chars": max_context_chars,
                "max_context_tokens": max_context_tokens,
                "compaction_count": state.compaction_count,
            },
            None,
        )
    record_event(
        "iteration_context_snapshot",
        {
            "iteration": state.iterations,
            "iteration_id": iteration_id,
            "context_hash": context_hash,
            "message_count": len(messages),
            "context_chars": _message_chars(list(messages)),
            "tool_count": len(tools),
        },
        None,
    )
    record_event(
        "model_turn_started",
        {
            "iteration": state.iterations,
            "iteration_id": iteration_id,
            "force_finish": force_finish,
            "tool_count": len(tools),
        },
        None,
    )
    try:
        turn = model_runner(request)
    except Exception as exc:
        if bool(getattr(exc, "cancelled", False)) or (is_cancelled and is_cancelled()):
            record_event(
                "run_cancelled",
                {"iteration": state.iterations, "iteration_id": iteration_id, "phase": "model_request"},
                None,
            )
            return AgentRunResult(
                status="cancelled",
                error={"code": "CANCELLED", "message": "run cancelled during model request"},
            )
        attempts = max(1, int(getattr(exc, "attempt_count", 1) or 1))
        state.model_retry_count += max(0, attempts - 1)
        record_event(
            "model_error",
            {
                "iteration": state.iterations,
                "iteration_id": iteration_id,
                "error_type": type(exc).__name__,
                "error_category": _model_error_category(exc),
                "attempt_count": attempts,
                "model_retry_count": state.model_retry_count,
            },
            None,
        )
        return AgentRunResult(
            status="failed",
            error={"code": "MODEL_ERROR", "message": "model request failed"},
        )
    state.model_retry_count += max(0, int(turn.attempt_count) - 1)
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        state.token_usage[key] = state.token_usage.get(key, 0) + max(0, int(turn.usage.get(key) or 0))
    record_event(
        "model_turn_completed",
        {
            "iteration": state.iterations,
            "iteration_id": iteration_id,
            "has_text": bool(turn.text),
            "tool_call_count": len(turn.tool_calls),
            "force_finish": force_finish,
            "finish_reason": turn.finish_reason,
            "usage": dict(turn.usage),
            "attempt_count": turn.attempt_count,
            "model_retry_count": state.model_retry_count,
            "usage_total": dict(state.token_usage),
        },
        None,
    )
    return turn


def _bounded_messages(
    messages: list[dict[str, Any]],
    *,
    max_chars: int,
    max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
) -> tuple[tuple[dict[str, Any], ...], int]:
    copied = [dict(item) for item in messages]
    effective_chars = min(max_chars, max_tokens * 4)
    if _message_chars(copied) <= effective_chars:
        return tuple(copied), 0

    system = [item for item in copied if str(item.get("role") or "") == "system"]
    conversation = [item for item in copied if str(item.get("role") or "") != "system"]
    groups = _message_groups(conversation)
    fixed = system
    notice = {
        "role": "system",
        "content": "Earlier conversation and tool details were compacted to stay within the model context budget.",
    }
    budget = max(0, effective_chars - _message_chars([*fixed, notice]) - 256)
    kept: list[list[dict[str, Any]]] = []
    used = 0
    for group in reversed(groups):
        if budget <= 0:
            break
        size = _message_chars(group)
        if kept and used + size > budget:
            break
        if size > budget:
            group = _clip_group(group, budget)
            size = _message_chars(group)
        kept.append(group)
        used += size
        if used >= budget:
            break
    kept.reverse()
    flattened = [item for group in kept for item in group]
    omitted = max(0, len(conversation) - len(flattened))
    return tuple([*fixed, notice, *flattened]), omitted


def _message_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        item = messages[index]
        group = [item]
        index += 1
        if str(item.get("role") or "") == "assistant" and item.get("tool_calls"):
            while index < len(messages) and str(messages[index].get("role") or "") == "tool":
                group.append(messages[index])
                index += 1
        groups.append(group)
    return groups


def _clip_group(group: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    if len(group) > 1:
        tool_count = max(1, len(group) - 1)
        assistant_chars = _message_chars([dict(group[0])])
        per_tool_chars = max(64, (max_chars - assistant_chars - 64 * tool_count) // tool_count)

        def compact(per_tool: int) -> list[dict[str, Any]]:
            compacted = [dict(group[0])]
            for message in group[1:]:
                item = dict(message)
                try:
                    payload = json.loads(str(item.get("content") or "{}"))
                except Exception:
                    payload = {}
                if isinstance(payload, dict):
                    payload = _compact_json_value(payload, max_chars=per_tool)
                    payload["context_compacted"] = True
                    item["content"] = json.dumps(payload, ensure_ascii=False, default=str)
                compacted.append(item)
            return compacted

        compacted = compact(per_tool_chars)
        while _message_chars(compacted) > max_chars and per_tool_chars > 64:
            per_tool_chars = max(64, per_tool_chars - max(32, _message_chars(compacted) - max_chars))
            compacted = compact(per_tool_chars)
        return compacted
    item = dict(group[0])
    content = str(item.get("content") or "")
    if len(content) > max_chars:
        item["content"] = content[: max(0, max_chars - 32)] + "\n[content truncated]"
    return [item]


def _message_chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(item, ensure_ascii=False, default=str)) for item in messages)


def _compact_json_value(value: Any, *, max_chars: int, depth: int = 0) -> Any:
    if depth >= 4:
        return _clip_text(value, min(240, max_chars))
    if isinstance(value, dict):
        priority = ("error", "message", "hint", "account", "currency", "month", "period", "source", "truncation")
        ordered = [key for key in priority if key in value]
        ordered.extend(key for key in value if key not in ordered)
        result: dict[str, Any] = {}
        for key in ordered[:20]:
            result[str(key)] = _compact_json_value(
                value[key],
                max_chars=max(96, max_chars // max(1, min(len(ordered), 8))),
                depth=depth + 1,
            )
        if len(ordered) > 20:
            result["_omitted_fields"] = len(ordered) - 20
        return result
    if isinstance(value, list):
        kept = value[:8]
        result = [
            _compact_json_value(item, max_chars=max(96, max_chars // max(1, len(kept))), depth=depth + 1)
            for item in kept
        ]
        if len(value) > len(kept):
            result.append({"_omitted_items": len(value) - len(kept)})
        return result
    if isinstance(value, str):
        return _clip_text(value, max(64, min(500, max_chars)))
    return value


def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 16)] + "...[truncated]"


def _model_tools(state: AgentState) -> list[dict[str, Any]]:
    tools = [dict(item) for item in state.manifest.tool_descriptions]
    if state.result_pages:
        tools.append(
            {
                "name": "__read_observation__",
                "description": "Read the next page of a truncated prior tool result.",
                "input_schema": {
                    "type": "object",
                    "properties": {"continuation_token": {"type": "string"}},
                    "required": ["continuation_token"],
                    "additionalProperties": False,
                },
            }
        )
    return tools


def _append_assistant_tool_calls(state: AgentState, text: str, calls: tuple[ToolCall, ...]) -> None:
    state.messages.append(
        {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {"id": call.call_id, "name": call.name, "arguments": dict(call.arguments)} for call in calls
            ],
        }
    )


def _execute_tool_call(
    state: AgentState,
    call: ToolCall,
    *,
    scene_input: dict[str, Any],
    build_tool_payload: ToolPayloadBuilder,
    call_read_tool: ReadToolCaller,
    compact_observation: ObservationCompactor,
    record_event: EventRecorder,
    is_cancelled: CancellationChecker | None,
) -> dict[str, Any]:
    if is_cancelled and is_cancelled():
        return _append_tool_observation(
            state,
            call,
            _error_observation(call.name, "CANCELLED", "run cancelled before tool execution"),
            record_event,
        )
    if call.name == "__read_observation__":
        state.tool_calls += 1
        record_event(
            "tool_call",
            {
                "iteration": state.iterations,
                "iteration_id": state.current_iteration_id,
                "tool_call_id": call.call_id,
                "tool_name": call.name,
                "tool_input": dict(call.arguments),
            },
            None,
        )
        observation = _read_observation_page(state, call.arguments)
        return _append_tool_observation(state, call, observation, record_event)
    if call.name not in state.manifest.allowed_tools:
        return _append_tool_observation(
            state,
            call,
            _error_observation(
                call.name,
                "POLICY_ERROR",
                "tool is outside the Host allowlist",
                hint="Choose one of the tools supplied in the current model request.",
            ),
            record_event,
        )
    if "__invalid_arguments__" in call.arguments:
        record_event(
            "tool_protocol_error",
            {
                "iteration": state.iterations,
                "iteration_id": state.current_iteration_id,
                "tool_call_id": call.call_id,
                "error_category": "malformed_arguments",
                "partial_tool_name": call.name,
                "partial_arguments": _clip_text(call.arguments.get("__invalid_arguments__"), 500),
            },
            None,
        )
        return _append_tool_observation(
            state,
            call,
            _error_observation(
                call.name,
                "INPUT_ERROR",
                "tool arguments are not valid JSON",
                hint="Retry once with a valid JSON object matching the tool schema.",
            ),
            record_event,
        )
    payload, payload_error = build_tool_payload(call.name, dict(call.arguments), scene_input)
    if payload_error or payload is None:
        return _append_tool_observation(
            state,
            call,
            _error_observation(
                call.name,
                "INPUT_ERROR",
                payload_error or "tool input could not be prepared",
                hint="Correct the arguments using the tool schema or choose another read-only tool.",
            ),
            record_event,
        )
    signature = json.dumps([call.name, payload], ensure_ascii=False, sort_keys=True, default=str)
    if signature in state.call_signatures:
        previous_error = state.call_outcomes.get(signature, "")
        previous_observation = state.successful_observations.get(signature)
        if previous_error == "SUCCESS" and previous_observation is not None:
            reused = {
                key: value
                for key, value in previous_observation.items()
                if key not in {"ref", "tool_call_id"}
            }
            reused["reused"] = True
            reused["reused_from_ref"] = previous_observation.get("ref")
            record_event(
                "tool_result_reused",
                {
                    "iteration": state.iterations,
                    "iteration_id": state.current_iteration_id,
                    "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "reused_from_ref": previous_observation.get("ref"),
                },
                str(previous_observation.get("ref") or "") or None,
            )
            return _append_tool_observation(state, call, reused, record_event)
        if previous_error not in TRANSIENT_TOOL_ERRORS or state.call_attempts.get(signature, 0) >= 2:
            return _append_tool_observation(
                state,
                call,
                _error_observation(
                    call.name,
                    "DUPLICATE_TOOL_CALL",
                    "identical tool call was already attempted",
                    hint="Reuse the prior result or change the arguments only if new information is needed.",
                ),
                record_event,
            )
    state.call_signatures.add(signature)
    state.call_attempts[signature] = state.call_attempts.get(signature, 0) + 1
    state.tool_calls += 1
    record_event(
        "tool_call",
        {
            "iteration": state.iterations,
            "iteration_id": state.current_iteration_id,
            "tool_call_id": call.call_id,
            "tool_name": call.name,
            "tool_input": payload,
        },
        None,
    )
    try:
        response = call_read_tool(call.name, payload)
    except SystemExit:
        response = {"ok": False, "error": {"code": "CONFIG_ERROR", "message": "tool configuration rejected"}}
    except Exception:
        response = {"ok": False, "error": {"code": "TOOL_EXCEPTION", "message": "tool raised an exception"}}
    if is_cancelled and is_cancelled():
        return _append_tool_observation(
            state,
            call,
            _error_observation(call.name, "CANCELLED", "run cancelled during tool execution"),
            record_event,
        )
    try:
        observation = compact_observation(call.name, response, payload)
    except Exception:
        observation = _error_observation(call.name, "OBSERVATION_ERROR", "tool result could not be normalized")
    observation["tool_input"] = payload
    _attach_result_page(state, observation, response)
    state.call_outcomes[signature] = str(observation.get("error") or "SUCCESS")
    appended = _append_tool_observation(state, call, observation, record_event)
    if appended.get("ok"):
        state.successful_observations[signature] = dict(appended)
    return appended


def _append_tool_observation(
    state: AgentState,
    call: ToolCall,
    observation: dict[str, Any],
    record_event: EventRecorder,
) -> dict[str, Any]:
    item = dict(observation)
    item.setdefault("ref", f"obs_{len(state.observations) + 1}")
    item.setdefault("tool_name", call.name)
    item.setdefault("tool_call_id", call.call_id)
    state.observations.append(item)
    state.messages.append(
        {
            "role": "tool",
            "tool_call_id": call.call_id,
            "name": call.name,
            "content": json.dumps(
                _project_observation_for_model(item),
                ensure_ascii=False,
                default=str,
            ),
        }
    )
    record_event("tool_result", item, str(item.get("ref") or "") or None)
    return item


def _project_observation_for_model(observation: dict[str, Any]) -> dict[str, Any]:
    if not bool(observation.get("ok")):
        return {
            key: observation[key]
            for key in ("error", "message", "hint")
            if observation.get(key) not in (None, "")
        }

    value = observation.get("value")
    if isinstance(value, dict):
        projected = dict(value)
    elif isinstance(value, list):
        projected = {"items": value}
    elif value is None:
        projected = {}
    else:
        projected = {"content": value}

    if not projected and observation.get("summary"):
        projected["summary"] = observation["summary"]
    truncation = observation.get("truncation")
    if isinstance(truncation, dict) and truncation:
        projected["truncation"] = dict(truncation)
    if observation.get("reused") is True:
        projected["reused"] = True
        projected["reused_from_ref"] = observation.get("reused_from_ref")
    return projected


def _attach_result_page(state: AgentState, observation: dict[str, Any], response: dict[str, Any]) -> None:
    raw = json.dumps(response.get("data") if isinstance(response, dict) else {}, ensure_ascii=False, default=str)
    if len(raw) <= OBSERVATION_PAGE_CHARS:
        return
    ref = f"obs_{len(state.observations) + 1}"
    state.result_pages[ref] = raw
    observation["truncation"] = {
        "next_action": "fetch_more",
        "continuation_token": f"{ref}:{OBSERVATION_PAGE_CHARS}",
    }


def _read_observation_page(state: AgentState, arguments: dict[str, Any]) -> dict[str, Any]:
    token = str(arguments.get("continuation_token") or "").strip()
    try:
        ref, offset_text = token.rsplit(":", 1)
        offset = int(offset_text)
        raw = state.result_pages[ref]
    except Exception:
        return _error_observation("__read_observation__", "INPUT_ERROR", "invalid continuation token")
    next_offset = offset + OBSERVATION_PAGE_CHARS
    return {
        "tool_name": "__read_observation__",
        "ok": True,
        "summary": f"continued observation {ref}",
        "value": {"content": raw[offset:next_offset]},
        **(
            {
                "truncation": {
                    "next_action": "fetch_more",
                    "continuation_token": f"{ref}:{next_offset}",
                }
            }
            if next_offset < len(raw)
            else {}
        ),
    }


def _load_fixture_observations(
    state: AgentState,
    fixture_id: str | None,
    fixture_observations: FixtureLoader,
    record_event: EventRecorder,
) -> None:
    try:
        items = [dict(item) for item in fixture_observations(fixture_id)]
    except Exception:
        items = [_error_observation("fixture", "FIXTURE_ERROR", "fixture observations could not be loaded")]
    for item in items:
        item.setdefault("ref", f"obs_{len(state.observations) + 1}")
        state.observations.append(item)
        record_event("fixture_observation", item, str(item.get("ref") or "") or None)
    state.messages.append(
        {
            "role": "system",
            "content": "Evaluation-only read observations:\n" + json.dumps(items, ensure_ascii=False, default=str),
        }
    )


def _load_recovered_observations(
    state: AgentState,
    observations: tuple[dict[str, Any], ...],
    record_event: EventRecorder,
) -> None:
    recovered = [dict(item) for item in observations if isinstance(item, dict) and item.get("tool_name")]
    if not recovered:
        return
    for item in recovered:
        item.setdefault("ref", f"recovered_{len(state.observations) + 1}")
        state.observations.append(item)
        payload = dict(item.get("tool_input") or {})
        signature = json.dumps([str(item.get("tool_name") or ""), payload], ensure_ascii=False, sort_keys=True, default=str)
        state.call_signatures.add(signature)
        state.call_attempts[signature] = 1
        state.call_outcomes[signature] = "SUCCESS" if item.get("ok") else str(item.get("error") or "ERROR")
        record_event("recovered_tool_result", item, str(item.get("ref") or "") or None)
    state.messages.append(
        {
            "role": "system",
            "content": (
                "Recovered read-only observations from the interrupted run. Reuse them and do not repeat an "
                "identical tool call unless the prior observation failed transiently.\n"
                + json.dumps([_project_observation_for_model(item) for item in recovered], ensure_ascii=False, default=str)
            ),
        }
    )


def _stop_reason(
    state: AgentState,
    *,
    max_tool_calls: int,
    timeout_seconds: float,
    reserve_seconds: float,
    started_at: float,
    clock: Clock,
    is_cancelled: CancellationChecker | None,
) -> tuple[str, dict[str, Any]] | None:
    if is_cancelled and is_cancelled():
        return "run_cancelled", {"reason": "cancellation_requested"}
    if timeout_seconds and clock() - started_at >= max(0.0, timeout_seconds - reserve_seconds):
        return "budget_exhausted", {"limit": "timeout_seconds"}
    if max_tool_calls and state.tool_calls >= max_tool_calls:
        return "budget_exhausted", {"limit": "max_tool_calls"}
    return None


def _remaining_seconds(timeout_seconds: float, started_at: float, clock: Clock) -> int | None:
    if timeout_seconds <= 0:
        return None
    return max(1, int(timeout_seconds - (clock() - started_at)))


def _joined_answer(parts: list[str], final_text: str) -> str:
    return "".join([*parts, final_text]).strip()


def _model_error_category(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in name or "timeout" in text:
        return "provider_timeout"
    status = getattr(exc, "http_status", None)
    if status == 429:
        return "provider_rate_limit"
    if isinstance(status, int) and status >= 500:
        return "provider_unavailable"
    if "context" in text and any(token in text for token in ("length", "window", "token")):
        return "context_overflow"
    return "provider_error"


def _error_observation(tool_name: str, code: str, message: str, *, hint: str | None = None) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "ok": False,
        "error": code,
        "message": message,
        **({"hint": hint} if hint else {}),
    }


__all__ = ["run_engine"]
