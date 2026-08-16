from __future__ import annotations

import json
import math
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

PROTOCOL = "om-pi-ipc.v1"
MAX_LINE_BYTES = 1_048_576
MAX_SAFE_MESSAGE_CHARS = 240
MIN_NODE_VERSION = (22, 19, 0)
MAX_FIXTURE_DELAY_MS = 300_000

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
        "tool_execution_start",
        "tool_execution_end",
        "turn_end",
        "agent_end",
    }
)

_STOP_REASONS = frozenset({"stop", "length", "aborted", "error"})

_StartPayload = dict[str, Any]
_Envelope = dict[str, Any]


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

    if payload["execution_environment"] != "eval":
        raise ValueError("S1 only accepts execution_environment 'eval'")
    if payload["session_id"] is not None:
        raise ValueError("S1 requires session_id null")
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

    if payload["tools"] != []:
        raise ValueError("S1 requires an empty tools array")
    if payload["recovered_observations"] != []:
        raise ValueError("S1 requires an empty recovered_observations array")

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
    if model["api_kind"] not in {"openai-responses", "openai-completions"}:
        raise ValueError("model.api_kind is not allowed")
    for key in (
        "timeout_seconds",
        "context_window_tokens",
        "max_output_tokens",
        "max_attempts",
    ):
        if not _is_pos_int(model[key]):
            raise ValueError(f"model.{key} must be a positive integer")

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
    if not isinstance(debug, dict) or set(debug) != {"fixture_response", "delay_ms"}:
        raise ValueError("debug must hold only fixture_response and delay_ms")
    if not isinstance(debug["fixture_response"], str):
        raise ValueError("debug.fixture_response must be a string")
    delay = debug["delay_ms"]
    if (
        not isinstance(delay, int)
        or isinstance(delay, bool)
        or delay < 0
        or delay > MAX_FIXTURE_DELAY_MS
    ):
        raise ValueError("debug.delay_ms must be an integer within [0, 300000]")


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
    line = json.dumps(record, ensure_ascii=False) + "\n"
    data = line.encode("utf-8")
    if len(data) > MAX_LINE_BYTES:
        raise ValueError("outbound envelope exceeds line ceiling")
    return data


def _decode_line(line: bytes) -> dict[str, Any] | None:
    if not line.endswith(b"\n"):
        return None
    if line.endswith(b"\r\n"):
        line = line[:-2] + b"\n"
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


def _validate_run_accepted(payload: dict[str, Any]) -> None:
    if set(payload) != {"runtime", "runtime_version", "session_id"}:
        raise ValueError("run.accepted payload shape is invalid")
    if payload["runtime"] != "pi-agent-core":
        raise ValueError("run.accepted runtime is not pi-agent-core")
    if payload["runtime_version"] != "0.84.2":
        raise ValueError("run.accepted runtime_version is not pinned")
    if payload["session_id"] is not None:
        raise ValueError("run.accepted session_id must be null")


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
    elif event_type in {"model_turn_completed", "turn_end"}:
        if set(data) != {"stop_reason", "usage"}:
            raise ValueError("turn event data shape is invalid")
        if data["stop_reason"] not in _STOP_REASONS:
            raise ValueError("turn event stop_reason is not allowed")
        _validate_usage(data["usage"])
    else:
        raise ValueError("tool events are not allowed in S1")


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
        raise ValueError("S1 terminal status is not allowed")
    if not isinstance(payload["text"], str):
        raise ValueError("terminal text must be a string")
    if payload["control_request"] is not None:
        raise ValueError("S1 terminal control_request must be null")
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
        raise ValueError("S1 proposal permits only an answered candidate")
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
    if not _is_nonempty_str(request_id) or not _is_nonempty_str(run_id):
        return _safe_failure("CONFIG_ERROR", "config", "invalid identity", False)
    if not _is_pos_int(timeout_seconds):
        return _safe_failure("CONFIG_ERROR", "config", "invalid timeout", False)

    try:
        _validate_start_payload(start_payload)
    except ValueError as exc:
        return _safe_failure("CONFIG_ERROR", "config", str(exc), False)

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
    decision_written = False
    decision: str | None = None
    stdout_buffer = b""
    final_result: dict[str, Any] | None = None
    final_error: dict[str, Any] | None = None
    final_ok: bool | None = None

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
                    if not chunk:
                        continue
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
                            # startup failure (non-zero, zero envelopes) is not
                            # mistaken for an in-run process death.
                            exit_code = process.poll()
                            _stop_child(process)
                            if exit_code not in (None, 0):
                                return _safe_failure(
                                    "PI_RUNTIME_UNAVAILABLE",
                                    "spawn",
                                    "child failed before protocol established",
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

                            if type_ == "run.accepted":
                                if accepted:
                                    _stop_child(process)
                                    return _safe_failure("PROTOCOL_ERROR", "protocol", "duplicate run.accepted", False)
                                _validate_run_accepted(payload)
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
                                _stop_child(process)
                                return _safe_failure("TOOL_BRIDGE_ERROR", "tool", "unexpected tool call", False)
                            elif type_ == "run.proposed":
                                if saw_terminal:
                                    _stop_child(process)
                                    return _safe_failure("PROTOCOL_ERROR", "protocol", "proposal after terminal", False)
                                _validate_terminal_payload(payload, final=False)
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
                                line_out = _encode_envelope(type_map[decision], payload_map[decision], identity, py_seq)
                                py_seq += 1
                                process.stdin.write(line_out)
                                process.stdin.flush()
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
