from __future__ import annotations

import json
import hashlib
import math
import os
import queue
import re
import selectors
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlsplit

PROTOCOL = "om-pi-ipc.v1"
MAX_LINE_BYTES = 1_048_576
MAX_SAFE_MESSAGE_CHARS = 240
MIN_NODE_VERSION = (22, 19, 0)
MAX_FIXTURE_DELAY_MS = 300_000
_SESSION_ID_PATTERN = re.compile(r"^om_[0-9a-f]{64}$")

_CHILD_ENV_ALLOW = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "OM_PI_MODEL_API_KEY",
        "OM_PI_SESSION_DB",
    }
)

_NODE_ERROR_CODES = frozenset(
    {
        "PROTOCOL_ERROR",
        "CONFIG_ERROR",
        "MODEL_ERROR",
        "SESSION_ERROR",
        "TOOL_BRIDGE_ERROR",
        "BUDGET_EXHAUSTED",
        "INTERNAL_ERROR",
    }
)

_NODE_ERROR_STAGES = frozenset(
    {"protocol", "config", "model", "session", "tool", "budget", "runtime"}
)

_EVENT_TYPES = frozenset(
    {
        "agent_start",
        "turn_start",
        "model_turn_completed",
        "context_compaction_committed",
        "forced_final_activated",
        "turn_end",
        "agent_end",
        "tool_execution_start",
        "tool_execution_end",
    }
)

_STOP_REASONS = frozenset(
    {"stop", "length", "toolUse", "aborted", "error", "deferred"}
)

_PROVIDER_API_KINDS = {
    "openai": "openai-responses",
    "deepseek": "openai-completions",
    "kimi": "openai-completions",
    "kimi-code": "openai-completions",
    "ollama": "openai-completions",
}

# Process-wide single active tool worker slot. A live worker owns the slot until
# its callback actually returns; a second run's tool call fails retryably while
# the slot is held (acceptance #10). Only the selector thread writes JSONL; the
# worker reports through a stdlib queue.
_TOOL_SLOT_LOCK = threading.Lock()
_TOOL_SLOT_BUSY = False

_StartPayload = dict[str, Any]
_Envelope = dict[str, Any]


def derive_pi_session_id(
    channel: str,
    sender: str,
    conversation: str,
    authority_scope: str,
) -> str:
    parts = (channel, sender, conversation, authority_scope)
    if any(not _is_nonempty_str(part) or "\0" in part for part in parts):
        raise ValueError("session identity parts must be non-empty and contain no NUL")
    material = "om-pi-session-v1\0" + "\0".join(parts)
    return "om_" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def derive_pi_local_session_id(authority_scope: str, session_key: str) -> str:
    parts = (authority_scope, session_key)
    if any(not _is_nonempty_str(part) or "\0" in part for part in parts):
        raise ValueError("local session identity parts must be non-empty and contain no NUL")
    material = "local\0" + authority_scope + "\0" + session_key
    return "om_" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _is_pos_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0


def _validate_usage(usage: Any) -> None:
    allowed = {"input", "output", "cacheRead", "cacheWrite", "totalTokens"}
    if not isinstance(usage, dict) or not set(usage) <= allowed:
        raise ValueError("usage has unknown fields")
    for value in usage.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("usage value must be a number")
        if not math.isfinite(float(value)) or value < 0:
            raise ValueError("usage value must be non-negative and finite")


def _safe_failure(
    code: str, stage: str, message: str, retryable: bool
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "stage": stage,
            "message": message[:MAX_SAFE_MESSAGE_CHARS],
            "retryable": bool(retryable),
        },
    }


def _stderr_summary(stderr_bytes: bytes) -> str:
    text = stderr_bytes.decode("utf-8", errors="replace").strip()
    return text if text else ""


def _validate_tools(tools: Any) -> None:
    # Empty is the S1 no-tools eval path; non-empty is the S2 tool bridge.
    if not isinstance(tools, list):
        raise ValueError("tools must be an array")
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {"name", "description", "input_schema"}:
            raise ValueError("tool must hold only name, description, input_schema")
        if not _is_nonempty_str(tool["name"]):
            raise ValueError("tool.name must be a non-empty string")
        if not _is_nonempty_str(tool["description"]):
            raise ValueError("tool.description must be a non-empty string")
        if not isinstance(tool["input_schema"], dict):
            raise ValueError("tool.input_schema must be an object")
        if tool["name"] in names:
            raise ValueError("tool names must be unique")
        names.add(tool["name"])


def _validate_fixture_turns(turns: Any) -> None:
    if not isinstance(turns, list):
        raise ValueError("debug.fixture_turns must be an array")
    for turn in turns:
        if not isinstance(turn, dict) or len(turn) != 1:
            raise ValueError("fixture turn must hold exactly one field")
        if "text" in turn:
            if not isinstance(turn["text"], str):
                raise ValueError("fixture turn text must be a string")
            continue
        if "tool_calls" not in turn:
            raise ValueError("fixture turn must hold text or tool_calls")
        calls = turn["tool_calls"]
        if not isinstance(calls, list) or not calls:
            raise ValueError("fixture turn tool_calls must be a non-empty array")
        for call in calls:
            if not isinstance(call, dict) or set(call) != {"call_id", "tool_name", "arguments"}:
                raise ValueError("fixture tool call must hold call_id, tool_name, arguments")
            if not _is_nonempty_str(call["call_id"]):
                raise ValueError("fixture tool call_id must be a non-empty string")
            if not _is_nonempty_str(call["tool_name"]):
                raise ValueError("fixture tool_name must be a non-empty string")
            if not isinstance(call["arguments"], dict):
                raise ValueError("fixture tool arguments must be an object")


def _validate_tool_call_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or set(payload) != {"call_id", "tool_name", "arguments"}:
        raise ValueError("tool.call payload must hold call_id, tool_name, arguments")
    if not _is_nonempty_str(payload["call_id"]):
        raise ValueError("tool.call call_id must be a non-empty string")
    if not _is_nonempty_str(payload["tool_name"]):
        raise ValueError("tool.call tool_name must be a non-empty string")
    if not isinstance(payload["arguments"], dict):
        raise ValueError("tool.call arguments must be an object")


def _run_tool_worker(
    call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    callback: Callable[[dict[str, Any]], dict[str, Any]],
    result_queue: "queue.Queue[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]",
) -> None:
    global _TOOL_SLOT_BUSY
    observation: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    try:
        try:
            observation = callback(
                {"call_id": call_id, "tool_name": tool_name, "arguments": arguments}
            )
        except BaseException:
            error = _safe_failure("TOOL_BRIDGE_ERROR", "tool", "tool callback failed", False)
        else:
            if not isinstance(observation, dict):
                error = _safe_failure("TOOL_BRIDGE_ERROR", "tool", "invalid callback return", False)
                observation = None
    finally:
        # Deliver before releasing the slot so a second run can never claim the
        # sole worker while this worker still owes a result.
        result_queue.put((call_id, observation, error))
        with _TOOL_SLOT_LOCK:
            _TOOL_SLOT_BUSY = False


def _validate_start_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("start payload must be an object")

    allowed_keys = {
        "execution_environment",
        "session_id",
        "system_prompt",
        "runtime_context",
        "user_message",
        "model",
        "tools",
        "limits",
        "recovered_observations",
        "debug",
    }
    if set(payload) != allowed_keys:
        raise ValueError("start payload has unknown or missing top-level fields")

    execution_environment = payload["execution_environment"]
    if execution_environment not in {"local", "eval", "channel"}:
        raise ValueError("execution_environment is not allowed")
    session_id = payload["session_id"]
    if session_id is not None and (
        not _is_nonempty_str(session_id) or _SESSION_ID_PATTERN.fullmatch(session_id) is None
    ):
        raise ValueError("session_id must be null or OM-derived")
    if not _is_nonempty_str(payload["system_prompt"]):
        raise ValueError("system_prompt must be a non-empty string")
    if not _is_nonempty_str(payload["user_message"]):
        raise ValueError("user_message must be a non-empty string")

    runtime_context = payload["runtime_context"]
    if not isinstance(runtime_context, list):
        raise ValueError("runtime_context must be an array")
    for item in runtime_context:
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "content"}
            or item.get("role") != "system"
            or not _is_nonempty_str(item.get("content"))
        ):
            raise ValueError("runtime_context must hold closed system messages")

    _validate_tools(payload["tools"])
    recovered = payload["recovered_observations"]
    if not isinstance(recovered, list) or any(
        not isinstance(observation, dict) for observation in recovered
    ):
        raise ValueError("recovered_observations must be an array of objects")

    model = payload["model"]
    if not isinstance(model, dict):
        raise ValueError("model must be an object")
    model_allowed = {
        "provider",
        "api_kind",
        "model",
        "base_url",
        "timeout_seconds",
        "context_window_tokens",
        "max_output_tokens",
        "max_attempts",
    }
    if set(model) != model_allowed:
        raise ValueError("model has unknown or missing fields")
    for key in ("provider", "model", "base_url"):
        if not _is_nonempty_str(model[key]):
            raise ValueError(f"model.{key} must be a non-empty string")
    if _PROVIDER_API_KINDS.get(model["provider"]) != model["api_kind"]:
        raise ValueError("model provider/API pair is not allowed")
    bounds = {
        "timeout_seconds": (1, 120),
        "context_window_tokens": (4_096, 2_000_000),
        "max_output_tokens": (64, 4_096),
        "max_attempts": (1, 3),
    }
    for key, (minimum, maximum) in bounds.items():
        if not _is_pos_int(model[key]) or not minimum <= model[key] <= maximum:
            raise ValueError(f"model.{key} is outside the allowed range")
    if model["context_window_tokens"] <= model["max_output_tokens"] + 2_000:
        raise ValueError(
            "model.context_window_tokens must exceed max_output_tokens by more than 2000"
        )
    parsed_url = urlsplit(model["base_url"])
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("model.base_url must be an HTTP(S) URL")

    limits = payload["limits"]
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    limits_allowed = {
        "timeout_seconds",
        "max_iterations",
        "max_tool_calls",
        "max_context_tokens",
        "max_consecutive_failed_tool_batches",
        "final_answer_reserve_seconds",
    }
    if set(limits) != limits_allowed:
        raise ValueError("limits has unknown or missing fields")
    for key in limits_allowed:
        if not _is_pos_int(limits[key]):
            raise ValueError(f"limits.{key} must be a positive integer")

    debug = payload["debug"]
    if execution_environment != "eval":
        if debug is not None:
            raise ValueError("debug must be null outside eval")
        return
    if not isinstance(debug, dict):
        raise ValueError("eval debug must be an object")
    if "delay_ms" not in debug:
        raise ValueError("debug.delay_ms is required")
    # S1 accepted a single string fixture; S2 adds a turn array. Keep both so
    # the no-tools eval path stays backward compatible.
    common_debug_keys = {
        "delay_ms",
        "persist_delay_ms",
        "expected_history",
        "forbidden_history",
        "compaction_response",
    }
    if "fixture_response" in debug:
        if not set(debug) <= common_debug_keys | {"fixture_response"}:
            raise ValueError("debug has unknown fields")
        if not isinstance(debug["fixture_response"], str):
            raise ValueError("debug.fixture_response must be a string")
    elif "fixture_turns" in debug:
        if not set(debug) <= common_debug_keys | {"fixture_turns"}:
            raise ValueError("debug has unknown fields")
        _validate_fixture_turns(debug["fixture_turns"])
    else:
        raise ValueError("debug must hold fixture_response or fixture_turns")
    if "fixture_response" in debug and "fixture_turns" in debug:
        raise ValueError("debug must hold exactly one fixture kind")
    for key in ("expected_history", "forbidden_history"):
        if key in debug and (
            not isinstance(debug[key], list)
            or any(not isinstance(item, str) for item in debug[key])
        ):
            raise ValueError(f"debug.{key} must be an array of strings")
    if "compaction_response" in debug and not isinstance(
        debug["compaction_response"], str
    ):
        raise ValueError("debug.compaction_response must be a string")
    delay = debug["delay_ms"]
    if (
        not isinstance(delay, int)
        or isinstance(delay, bool)
        or delay < 0
        or delay > MAX_FIXTURE_DELAY_MS
    ):
        raise ValueError("debug.delay_ms must be an integer within [0, 300000]")
    persist_delay = debug.get("persist_delay_ms", 0)
    if (
        not isinstance(persist_delay, int)
        or isinstance(persist_delay, bool)
        or persist_delay < 0
        or persist_delay > 300_000
    ):
        raise ValueError(
            "debug.persist_delay_ms must be an integer within [0, 300000]"
        )


def _child_env(environ: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    child = {}
    for key in _CHILD_ENV_ALLOW:
        value = source.get(key)
        if value:
            child[key] = value
    return child


def _runtime_command(
    runtime_entry: Path | None, environ: Mapping[str, str] | None
) -> tuple[list[str], Path]:
    source = os.environ if environ is None else environ
    node = shutil.which("node", path=source.get("PATH"))
    if node is None:
        raise LookupError("node executable not found")
    try:
        version_out = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        raise LookupError("node version probe failed")
    if not version_out.startswith("v"):
        raise LookupError("node version output is unparseable")
    try:
        parts = version_out[1:].split(".")
        numeric = tuple(int(part) for part in parts[:3])
    except ValueError:
        raise LookupError("node version output is unparseable")
    if numeric < MIN_NODE_VERSION:
        raise LookupError("node is older than 22.19.0")

    if runtime_entry is None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        entry = repo_root / "agent-runtime" / "main.ts"
    else:
        entry = runtime_entry
    if not entry.is_file():
        raise LookupError("runtime entry is missing")
    return [node, str(entry)], entry


def _encode_envelope(
    type_: str, payload: dict[str, Any], identity: dict[str, str], seq: int
) -> bytes:
    record = {
        "protocol": PROTOCOL,
        "type": type_,
        "request_id": identity["request_id"],
        "run_id": identity["run_id"],
        "seq": seq,
        "payload": payload,
    }
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise ValueError("outbound envelope is not JSON serializable") from exc
    data = line.encode("utf-8")
    if len(data) > MAX_LINE_BYTES:
        raise ValueError("outbound envelope exceeds line ceiling")
    return data


def _decode_line(line: bytes) -> dict[str, Any] | None:
    if not line.endswith(b"\n"):
        return None
    body = line.rstrip(b"\r\n")
    if not body:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("invalid UTF-8")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("malformed JSON")
    if not isinstance(obj, dict):
        raise ValueError("record is not an object")
    return obj


def _validate_envelope(
    obj: dict[str, Any],
    expected_seq: int,
    identity: dict[str, str],
    allowed_types: frozenset[str],
) -> str:
    if set(obj) != {"protocol", "type", "request_id", "run_id", "seq", "payload"}:
        raise ValueError("record has unknown or missing envelope fields")
    if obj["protocol"] != PROTOCOL:
        raise ValueError("unknown protocol")
    type_ = obj["type"]
    if not _is_nonempty_str(type_) or type_ not in allowed_types:
        raise ValueError("unknown or empty type")
    if obj["request_id"] != identity["request_id"]:
        raise ValueError("mismatched request_id")
    if obj["run_id"] != identity["run_id"]:
        raise ValueError("mismatched run_id")
    if obj["seq"] != expected_seq:
        raise ValueError("sequence is not contiguous")
    if not isinstance(obj["payload"], dict):
        raise ValueError("payload is not an object")
    return type_


def _stop_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _validate_run_accepted(payload: dict[str, Any], expected_session_id: str | None) -> None:
    if set(payload) != {"runtime", "runtime_version", "session_id"}:
        raise ValueError("run.accepted payload shape is invalid")
    if payload["runtime"] != "pi-agent-core":
        raise ValueError("run.accepted runtime is not pi-agent-core")
    if payload["runtime_version"] != "0.84.2":
        raise ValueError("run.accepted runtime_version is not pinned")
    if payload["session_id"] != expected_session_id:
        raise ValueError("run.accepted session_id does not match run.start")


def _validate_agent_event(payload: dict[str, Any]) -> None:
    if set(payload) != {"event_type", "data"}:
        raise ValueError("agent.event payload shape is invalid")
    event_type = payload["event_type"]
    if event_type not in _EVENT_TYPES:
        raise ValueError("agent.event event_type is not allowed")
    data = payload["data"]
    if not isinstance(data, dict):
        raise ValueError("agent.event data must be an object")
    if event_type in {"agent_start", "turn_start", "agent_end"}:
        if data != {}:
            raise ValueError("lifecycle event data must be empty")
    elif event_type == "model_turn_completed":
        if set(data) != {
            "stop_reason",
            "attempt_count",
            "model_retry_count",
            "usage",
            "usage_total",
        }:
            raise ValueError("turn event data shape is invalid")
        if data["stop_reason"] not in _STOP_REASONS:
            raise ValueError("turn event stop_reason is not allowed")
        for key in ("attempt_count", "model_retry_count"):
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"turn event {key} must be a non-negative integer")
        _validate_usage(data["usage"])
        _validate_usage(data["usage_total"])
    elif event_type == "forced_final_activated":
        if set(data) != {"reason"} or data["reason"] not in {
            "model_turn_limit",
            "tool_call_limit",
            "tool_failure_limit",
            "time_reserve",
        }:
            raise ValueError("forced-final event data shape is invalid")
    elif event_type == "context_compaction_committed":
        if set(data) != {"compaction_count", "usage_total"}:
            raise ValueError("compaction event data shape is invalid")
        count = data["compaction_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("compaction_count must be a positive integer")
        _validate_usage(data["usage_total"])
    elif event_type == "turn_end":
        if set(data) != {"stop_reason", "usage"}:
            raise ValueError("turn event data shape is invalid")
        if data["stop_reason"] not in _STOP_REASONS:
            raise ValueError("turn event stop_reason is not allowed")
        _validate_usage(data["usage"])
    elif event_type == "tool_execution_start":
        if set(data) != {"call_id", "tool_name"}:
            raise ValueError("tool_execution_start data shape is invalid")
        if not _is_nonempty_str(data["call_id"]) or not _is_nonempty_str(data["tool_name"]):
            raise ValueError("tool event ids must be non-empty strings")
    elif event_type == "tool_execution_end":
        if set(data) != {"call_id", "tool_name", "ok"}:
            raise ValueError("tool_execution_end data shape is invalid")
        if not _is_nonempty_str(data["call_id"]) or not _is_nonempty_str(data["tool_name"]):
            raise ValueError("tool event ids must be non-empty strings")
        if not isinstance(data["ok"], bool):
            raise ValueError("tool_execution_end ok must be a boolean")


def _validate_terminal_payload(payload: dict[str, Any], final: bool) -> None:
    status = payload.get("status")
    if final:
        required = {
            "status",
            "text",
            "control_request",
            "termination_reason",
            "usage",
            "committed",
        }
    else:
        required = {
            "status",
            "text",
            "control_request",
            "termination_reason",
            "usage",
        }
    if set(payload) != required:
        raise ValueError("terminal payload has unknown or missing fields")
    if status not in {"answered", "cancelled"}:
        raise ValueError("S1/S2 terminal status is not allowed")
    if not isinstance(payload["text"], str):
        raise ValueError("terminal text must be a string")
    if payload["control_request"] is not None:
        raise ValueError("S1/S2 terminal control_request must be null")
    reason = payload["termination_reason"]
    _validate_usage(payload["usage"])
    if status == "answered":
        if reason not in {"stop", "length"}:
            raise ValueError("answered termination_reason must be stop or length")
    else:
        if reason != "aborted":
            raise ValueError("cancelled termination_reason must be aborted")
        if payload["text"] != "":
            raise ValueError("cancelled terminal text must be empty")
    if not final and status != "answered":
        raise ValueError("S1/S2 proposal permits only an answered candidate")
    if final:
        if not isinstance(payload["committed"], bool):
            raise ValueError("run.final committed must be a boolean")


def _validate_run_error(payload: dict[str, Any]) -> None:
    if set(payload) != {"code", "stage", "message", "retryable"}:
        raise ValueError("run.error payload shape is invalid")
    if payload["code"] not in _NODE_ERROR_CODES:
        raise ValueError("run.error code is not allowed")
    if payload["stage"] not in _NODE_ERROR_STAGES:
        raise ValueError("run.error stage is not allowed")
    if not isinstance(payload["message"], str) or len(payload["message"]) > MAX_SAFE_MESSAGE_CHARS:
        raise ValueError("run.error message is not a bounded string")
    if not isinstance(payload["retryable"], bool):
        raise ValueError("run.error retryable must be a boolean")


def run_pi_agent(
    start_payload: dict[str, Any],
    *,
    request_id: str,
    run_id: str,
    timeout_seconds: int,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_tool_call: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    on_proposed: Callable[
        [dict[str, Any]], Literal["commit", "discard", "cancel"]
    ]
    | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    runtime_entry: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    global _TOOL_SLOT_BUSY

    if not _is_nonempty_str(request_id) or not _is_nonempty_str(run_id):
        return _safe_failure("CONFIG_ERROR", "config", "invalid identity", False)
    if not _is_pos_int(timeout_seconds):
        return _safe_failure("CONFIG_ERROR", "config", "invalid timeout", False)

    try:
        _validate_start_payload(start_payload)
    except ValueError as exc:
        return _safe_failure("CONFIG_ERROR", "config", str(exc), False)

    allowed_tool_names = frozenset(tool["name"] for tool in start_payload["tools"])

    if start_payload["limits"]["timeout_seconds"] != timeout_seconds:
        return _safe_failure(
            "CONFIG_ERROR", "config", "timeout mismatch with limits", False
        )
    if (
        start_payload["model"]["timeout_seconds"]
        > start_payload["limits"]["timeout_seconds"]
    ):
        return _safe_failure(
            "CONFIG_ERROR", "config", "model timeout exceeds scene timeout", False
        )

    deadline = time.monotonic() + timeout_seconds

    if is_cancelled is not None:
        try:
            if is_cancelled():
                return _safe_failure("CANCELLED", "cancel", "cancelled before spawn", False)
        except Exception:
            return _safe_failure("INTERNAL_ERROR", "runtime", "cancellation check failed", False)

    try:
        command, entry = _runtime_command(runtime_entry, environ)
    except LookupError as exc:
        return _safe_failure("PI_RUNTIME_UNAVAILABLE", "spawn", str(exc), False)

    identity = {"request_id": request_id, "run_id": run_id}
    try:
        start_line = _encode_envelope("run.start", start_payload, identity, 1)
    except ValueError as exc:
        return _safe_failure("CONFIG_ERROR", "config", str(exc), False)

    child_env = _child_env(environ)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(entry.parent.parent if runtime_entry is None else entry.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            bufsize=0,
        )
    except OSError as exc:
        return _safe_failure("PI_RUNTIME_UNAVAILABLE", "spawn", "failed to spawn child", False)

    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)

    sel = selectors.DefaultSelector()
    sel.register(process.stdout, selectors.EVENT_READ)
    sel.register(process.stderr, selectors.EVENT_READ)

    node_seq = 1
    py_seq = 2
    accepted = False
    saw_terminal = False
    cancel_sent = False
    cancel_deadline: float | None = None
    post_terminal_deadline: float | None = None
    proposal_seen = False
    decision_written = False
    decision: str | None = None
    stdout_buffer = b""
    stderr_buffer = b""
    final_result: dict[str, Any] | None = None
    final_error: dict[str, Any] | None = None
    final_ok: bool | None = None
    result_queue: "queue.Queue[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]" = queue.Queue()
    pending_tool: dict[str, str] | None = None

    try:
        process.stdin.write(start_line)
        process.stdin.flush()

        while True:
            if is_cancelled is not None and not cancel_sent and not decision_written and not saw_terminal:
                try:
                    if is_cancelled():
                        cancel_line = _encode_envelope(
                            "run.cancel", {"reason": "host_cancel_requested"}, identity, py_seq
                        )
                        py_seq += 1
                        process.stdin.write(cancel_line)
                        process.stdin.flush()
                        cancel_sent = True
                        cancel_deadline = time.monotonic() + 2
                except (OSError, ValueError):
                    _stop_child(process)
                    return _safe_failure("INTERNAL_ERROR", "runtime", "cancellation write failed", False)

            now = time.monotonic()
            if cancel_deadline is not None and now >= cancel_deadline and not saw_terminal:
                _stop_child(process)
                return _safe_failure("CANCELLED", "cancel", "cancellation grace expired", False)
            if now >= deadline and not saw_terminal:
                if not cancel_sent:
                    try:
                        cancel_line = _encode_envelope(
                            "run.cancel", {"reason": "deadline"}, identity, py_seq
                        )
                        process.stdin.write(cancel_line)
                        process.stdin.flush()
                        cancel_sent = True
                    except (OSError, ValueError):
                        pass
                _stop_child(process)
                return _safe_failure("PI_PROCESS_TIMEOUT", "deadline", "Pi Agent timed out", True)
            if saw_terminal and post_terminal_deadline is not None and now >= post_terminal_deadline:
                _stop_child(process)
                break

            timeout = 0.1
            if cancel_deadline is not None:
                timeout = min(timeout, max(0.0, cancel_deadline - now))
            timeout = min(timeout, max(0.0, deadline - now))
            if post_terminal_deadline is not None:
                timeout = min(timeout, max(0.0, post_terminal_deadline - now))

            events = sel.select(timeout)
            for key, _ in events:
                if key.fileobj is process.stderr:
                    try:
                        chunk = os.read(process.stderr.fileno(), 65536)
                    except (BlockingIOError, OSError):
                        chunk = b""
                    if chunk:
                        stderr_buffer += chunk
                    continue
                if key.fileobj is process.stdout:
                    try:
                        chunk = os.read(process.stdout.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if not chunk:
                        sel.unregister(process.stdout)
                        # child closed stdout: EOF
                        if not saw_terminal:
                            # Capture the exit code before reaping so a hard
                            # startup failure (non-zero, before acceptance) is
                            # not mistaken for an in-run process death. Once
                            # `run.accepted` was seen the protocol is
                            # established, so a non-zero exit is a process
                            # death, not a runtime availability failure.
                            exit_code = process.poll()
                            try:
                                while True:
                                    tail = os.read(process.stderr.fileno(), 65536)
                                    if not tail:
                                        break
                                    stderr_buffer += tail
                            except (BlockingIOError, OSError):
                                pass
                            _stop_child(process)
                            if exit_code not in (None, 0) and not accepted:
                                detail = _stderr_summary(stderr_buffer)
                                message = "child failed before protocol established"
                                if detail:
                                    message = f"{message}: {detail}"
                                return _safe_failure(
                                    "PI_RUNTIME_UNAVAILABLE",
                                    "spawn",
                                    message,
                                    False,
                                )
                            return _safe_failure("PI_PROCESS_EXITED", "process", "child exited before terminal", True)
                        continue
                    stdout_buffer += chunk
                    if len(stdout_buffer) > MAX_LINE_BYTES:
                        _stop_child(process)
                        return _safe_failure("PROTOCOL_ERROR", "protocol", "line exceeds ceiling", False)
                    while b"\n" in stdout_buffer:
                        line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                        line += b"\n"
                        try:
                            obj = _decode_line(line)
                            if obj is None:
                                _stop_child(process)
                                return _safe_failure("PROTOCOL_ERROR", "protocol", "blank record", False)
                            if saw_terminal:
                                _stop_child(process)
                                return _safe_failure("PROTOCOL_ERROR", "protocol", "record after terminal", False)
                            type_ = _validate_envelope(
                                obj, node_seq, identity, frozenset({
                                    "run.accepted", "agent.event", "tool.call",
                                    "run.proposed", "run.final", "run.error",
                                })
                            )
                            node_seq += 1
                            payload = obj["payload"]
                            if not accepted and type_ not in {"run.accepted", "run.error"}:
                                _stop_child(process)
                                return _safe_failure(
                                    "PROTOCOL_ERROR",
                                    "protocol",
                                    "record before run.accepted",
                                    False,
                                )

                            if type_ == "run.accepted":
                                if accepted:
                                    _stop_child(process)
                                    return _safe_failure("PROTOCOL_ERROR", "protocol", "duplicate run.accepted", False)
                                _validate_run_accepted(
                                    payload, start_payload["session_id"]
                                )
                                accepted = True
                            elif type_ == "agent.event":
                                if not accepted:
                                    _stop_child(process)
                                    return _safe_failure("PROTOCOL_ERROR", "protocol", "event before accepted", False)
                                _validate_agent_event(payload)
                                if on_event is not None:
                                    try:
                                        on_event(payload)
                                    except Exception:
                                        _stop_child(process)
                                        return _safe_failure("INTERNAL_ERROR", "runtime", "event callback failed", False)
                            elif type_ == "tool.call":
                                if not accepted:
                                    _stop_child(process)
                                    return _safe_failure("PROTOCOL_ERROR", "protocol", "tool call before accepted", False)
                                _validate_tool_call_payload(payload)
                                if payload["tool_name"] not in allowed_tool_names:
                                    _stop_child(process)
                                    return _safe_failure("TOOL_BRIDGE_ERROR", "tool", "tool outside Host allowlist", False)
                                if on_tool_call is None:
                                    _stop_child(process)
                                    return _safe_failure("INTERNAL_ERROR", "runtime", "missing tool callback", False)
                                if pending_tool is not None:
                                    _stop_child(process)
                                    return _safe_failure("PROTOCOL_ERROR", "protocol", "second outstanding tool call", False)
                                with _TOOL_SLOT_LOCK:
                                    if _TOOL_SLOT_BUSY:
                                        _stop_child(process)
                                        return _safe_failure("TOOL_BRIDGE_ERROR", "tool", "another tool call is outstanding", True)
                                    _TOOL_SLOT_BUSY = True
                                pending_tool = {
                                    "call_id": payload["call_id"],
                                    "tool_name": payload["tool_name"],
                                }
                                try:
                                    worker = threading.Thread(
                                        target=_run_tool_worker,
                                        args=(
                                            payload["call_id"],
                                            payload["tool_name"],
                                            payload["arguments"],
                                            on_tool_call,
                                            result_queue,
                                        ),
                                        daemon=True,
                                    )
                                    worker.start()
                                except Exception:
                                    with _TOOL_SLOT_LOCK:
                                        _TOOL_SLOT_BUSY = False
                                    _stop_child(process)
                                    return _safe_failure("INTERNAL_ERROR", "runtime", "tool worker spawn failed", False)
                            elif type_ == "run.proposed":
                                if saw_terminal:
                                    _stop_child(process)
                                    return _safe_failure("PROTOCOL_ERROR", "protocol", "proposal after terminal", False)
                                if proposal_seen:
                                    _stop_child(process)
                                    return _safe_failure("PROTOCOL_ERROR", "protocol", "duplicate proposal", False)
                                proposal_seen = True
                                _validate_terminal_payload(payload, final=False)
                                if cancel_sent:
                                    # Host cancellation is already the durable
                                    # winner; the buffered proposal is superseded.
                                    # Do not open a second admission decision.
                                    continue
                                if on_proposed is None:
                                    _stop_child(process)
                                    return _safe_failure("INTERNAL_ERROR", "runtime", "missing proposal callback", False)
                                try:
                                    decision = on_proposed(payload)
                                except Exception:
                                    _stop_child(process)
                                    return _safe_failure("INTERNAL_ERROR", "runtime", "proposal callback failed", False)
                                if decision not in {"commit", "discard", "cancel"}:
                                    _stop_child(process)
                                    return _safe_failure("INTERNAL_ERROR", "runtime", "invalid proposal decision", False)
                                type_map = {"commit": "run.commit", "discard": "run.discard", "cancel": "run.cancel"}
                                payload_map = {"commit": {}, "discard": {}, "cancel": {"reason": "host_cancel_requested"}}
                                try:
                                    line_out = _encode_envelope(
                                        type_map[decision], payload_map[decision], identity, py_seq
                                    )
                                    py_seq += 1
                                    process.stdin.write(line_out)
                                    process.stdin.flush()
                                except ValueError:
                                    _stop_child(process)
                                    return _safe_failure(
                                        "PROTOCOL_ERROR",
                                        "protocol",
                                        "invalid admission decision envelope",
                                        False,
                                    )
                                except OSError:
                                    _stop_child(process)
                                    return _safe_failure(
                                        "PI_PROCESS_EXITED",
                                        "process",
                                        "child closed stdin before admission decision",
                                        True,
                                    )
                                decision_written = True
                                if decision == "cancel":
                                    cancel_sent = True
                                    cancel_deadline = time.monotonic() + 2
                            elif type_ == "run.final":
                                _validate_terminal_payload(payload, final=True)
                                if decision is not None:
                                    expected_committed = decision == "commit"
                                    expected_status = "cancelled" if decision == "cancel" else "answered"
                                    if payload["status"] != expected_status or payload["committed"] != expected_committed:
                                        _stop_child(process)
                                        return _safe_failure("PROTOCOL_ERROR", "protocol", "final does not match admission decision", False)
                                elif payload["committed"] is not False:
                                    _stop_child(process)
                                    return _safe_failure("PROTOCOL_ERROR", "protocol", "unproposed final must be uncommitted", False)
                                saw_terminal = True
                                post_terminal_deadline = time.monotonic() + 1
                                final_result = payload
                                final_ok = True
                            elif type_ == "run.error":
                                _validate_run_error(payload)
                                saw_terminal = True
                                post_terminal_deadline = time.monotonic() + 1
                                final_error = payload
                                final_ok = False
                        except ValueError:
                            _stop_child(process)
                            return _safe_failure("PROTOCOL_ERROR", "protocol", "invalid child record", False)

            while True:
                try:
                    call_id, observation, error = result_queue.get_nowait()
                except queue.Empty:
                    break
                if pending_tool is None or pending_tool["call_id"] != call_id:
                    _stop_child(process)
                    return _safe_failure("PROTOCOL_ERROR", "protocol", "tool result mismatch", False)
                if error is not None:
                    _stop_child(process)
                    return error
                if cancel_sent or saw_terminal:
                    pending_tool = None
                    continue
                tool_name = pending_tool["tool_name"]
                try:
                    line_out = _encode_envelope(
                        "tool.result",
                        {"call_id": call_id, "tool_name": tool_name, "observation": observation},
                        identity,
                        py_seq,
                    )
                    py_seq += 1
                    process.stdin.write(line_out)
                    process.stdin.flush()
                except ValueError:
                    _stop_child(process)
                    return _safe_failure(
                        "TOOL_BRIDGE_ERROR",
                        "tool",
                        "observation is invalid or exceeds line ceiling",
                        False,
                    )
                except OSError:
                    _stop_child(process)
                    return _safe_failure(
                        "PI_PROCESS_EXITED", "process", "child closed stdin before tool result", True
                    )
                pending_tool = None

            if saw_terminal and process.poll() is not None:
                break

        if final_ok and final_result is not None:
            return {"ok": True, "result": final_result}
        if final_error is not None:
            return {"ok": False, "error": final_error}
        return _safe_failure("PROTOCOL_ERROR", "protocol", "missing terminal", False)
    finally:
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except OSError:
            pass
        try:
            sel.close()
        except OSError:
            pass
        _stop_child(process)
