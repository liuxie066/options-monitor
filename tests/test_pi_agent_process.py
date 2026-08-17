from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.infrastructure.pi_agent_process import (  # noqa: E402
    _runtime_command,
    run_pi_agent,
)
from src.infrastructure import pi_agent_process as pi_process  # noqa: E402


def _start_payload(**overrides):
    base = {
        "execution_environment": "eval",
        "session_id": None,
        "system_prompt": "sys",
        "runtime_context": [],
        "user_message": "hi",
        "model": {
            "provider": "deepseek",
            "api_kind": "openai-completions",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "timeout_seconds": 30,
            "context_window_tokens": 24000,
            "max_output_tokens": 2048,
            "max_attempts": 2,
        },
        "tools": [],
        "limits": {
            "timeout_seconds": 60,
            "max_iterations": 16,
            "max_tool_calls": 12,
            "max_context_tokens": 24000,
            "max_consecutive_failed_tool_batches": 2,
            "final_answer_reserve_seconds": 20,
        },
        "recovered_observations": [],
        "debug": {"fixture_response": "hello", "delay_ms": 0},
    }
    base.update(overrides)
    return base


def _write_fake(tmp_path: Path, source: str) -> Path:
    entry = tmp_path / "fake.mjs"
    entry.write_text(source, encoding="utf-8")
    return entry


_READ_TOOL = {
    "name": "runtime_status",
    "description": "Read runtime status",
    "input_schema": {
        "type": "object",
        "properties": {"index": {"type": "integer"}},
        "additionalProperties": False,
    },
}


def _tool_payload(turns, **overrides):
    return _start_payload(
        tools=[_READ_TOOL],
        debug={"fixture_turns": turns, "delay_ms": 0},
        **overrides,
    )


def _tool_turn(
    call_id: str = "call_1",
    *,
    tool_name: str = "runtime_status",
    arguments: dict | None = None,
):
    return {
        "tool_calls": [
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "arguments": arguments or {},
            }
        ]
    }


def _wait_for_tool_slot(expected: bool, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with pi_process._TOOL_SLOT_LOCK:
            if pi_process._TOOL_SLOT_BUSY is expected:
                return True
        time.sleep(0.01)
    return False


def _node_protocol_case(messages):
    command, entry = _runtime_command(None, None)
    process = subprocess.Popen(
        command,
        cwd=entry.parent.parent,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    identity = {"request_id": "req_1", "run_id": "run_1"}
    start = {
        "protocol": "om-pi-ipc.v1",
        "type": "run.start",
        **identity,
        "seq": 1,
        "payload": _tool_payload(
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_1",
                            "tool_name": "runtime_status",
                            "arguments": {},
                        }
                    ]
                },
                {"text": "done"},
            ]
        ),
    }
    buffer = b""

    def read_record():
        nonlocal buffer
        deadline = time.monotonic() + 5
        while b"\n" not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("timed out waiting for Node record")
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                raise AssertionError("timed out waiting for Node record")
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise AssertionError("Node exited before terminal record")
            buffer += chunk
        line, buffer = buffer.split(b"\n", 1)
        return json.loads(line)

    try:
        process.stdin.write((json.dumps(start) + "\n").encode())
        process.stdin.flush()
        while read_record()["type"] != "tool.call":
            pass
        encoded = []
        for seq, (type_, payload) in enumerate(messages, start=2):
            encoded.append(
                json.dumps(
                    {
                        "protocol": "om-pi-ipc.v1",
                        "type": type_,
                        **identity,
                        "seq": seq,
                        "payload": payload,
                    }
                )
                + "\n"
            )
        process.stdin.write("".join(encoded).encode())
        process.stdin.flush()
        while True:
            record = read_record()
            if record["type"] in {"run.error", "run.final"}:
                return record
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def _fake_tool_call_child(calls) -> str:
    records = "\n".join(
        f'rec("tool.call", {seq}, {json.dumps(call)});'
        for seq, call in enumerate(calls, start=2)
    )
    return (
        """
import { createInterface } from "node:readline";
const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
const rec = (type, seq, payload) =>
  process.stdout.write(JSON.stringify({
    protocol: "om-pi-ipc.v1", type, request_id: "req_1", run_id: "run_1",
    seq, payload,
  }) + "\\n");
rl.once("line", () => {
  rec("run.accepted", 1, { runtime: "pi-agent-core", runtime_version: "0.84.2", session_id: null });
"""
        + records
        + "\n});\n"
    )


_HAPPY_CHILD = """
import { createInterface } from "node:readline";
const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
const rec = (type, seq, payload) =>
  process.stdout.write(JSON.stringify({
    protocol: "om-pi-ipc.v1", type, request_id: "req_1", run_id: "run_1",
    seq, payload,
  }) + "\\n");
let n = 0;
rl.on("line", (line) => {
  n += 1;
  if (n === 1) {
    rec("run.accepted", 1, { runtime: "pi-agent-core", runtime_version: "0.84.2", session_id: null });
    rec("agent.event", 2, { event_type: "agent_start", data: {} });
    rec("agent.event", 3, { event_type: "turn_start", data: {} });
    rec("agent.event", 4, { event_type: "model_turn_completed", data: { stop_reason: "stop", usage: { input: 1, output: 1, totalTokens: 2 } } });
    rec("agent.event", 5, { event_type: "turn_end", data: { stop_reason: "stop", usage: { input: 1, output: 1, totalTokens: 2 } } });
    rec("agent.event", 6, { event_type: "agent_end", data: {} });
    rec("run.proposed", 7, { status: "answered", text: "hello", control_request: null, termination_reason: "stop", usage: { input: 1, output: 1, totalTokens: 2 } });
  } else if (n === 2) {
    const decision = JSON.parse(line).type;
    const committed = decision === "run.commit";
    rec("run.final", 8, { status: "answered", text: "hello", control_request: null, termination_reason: "stop", usage: { input: 1, output: 1, totalTokens: 2 }, committed });
    process.exit(0);
  }
});
"""


def test_commit_and_discard():
    events = []
    committed = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_event=events.append,
        on_proposed=lambda p: "commit",
    )
    assert committed == {
        "ok": True,
        "result": {
            "status": "answered",
            "text": "hello",
            "control_request": None,
            "termination_reason": "stop",
            "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0},
            "committed": True,
        },
    }
    assert [e["event_type"] for e in events] == [
        "agent_start",
        "turn_start",
        "model_turn_completed",
        "turn_end",
        "agent_end",
    ]

    discarded = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_proposed=lambda p: "discard",
    )
    assert discarded["ok"] is True
    assert discarded["result"]["committed"] is False


def test_cancel_trace():
    import time

    t0 = time.monotonic()

    def is_cancelled():
        return time.monotonic() - t0 > 0.5

    result = run_pi_agent(
        _start_payload(debug={"fixture_response": "hello", "delay_ms": 5000}),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        is_cancelled=is_cancelled,
    )
    assert result["ok"] is True
    assert result["result"]["status"] == "cancelled"
    assert result["result"]["committed"] is False


def test_malformed_child_fails_protocol(tmp_path):
    entry = _write_fake(tmp_path, 'process.stdout.write("not json\\n");\n')
    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        runtime_entry=entry,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "PROTOCOL_ERROR"
    assert result["error"]["stage"] == "protocol"


def test_mismatched_run_id_fails_closed(tmp_path):
    entry = _write_fake(
        tmp_path,
        'process.stdout.write(JSON.stringify({protocol:"om-pi-ipc.v1",type:"run.accepted",request_id:"req_1",run_id:"OTHER",seq:1,payload:{runtime:"pi-agent-core",runtime_version:"0.84.2",session_id:null}})+"\\n");\n',
    )
    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        runtime_entry=entry,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "PROTOCOL_ERROR"


def test_premature_eof(tmp_path):
    entry = _write_fake(tmp_path, "process.exit(0);\n")
    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        runtime_entry=entry,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "PI_PROCESS_EXITED"
    assert result["error"]["retryable"] is True


def test_pre_identity_nonzero_exit(tmp_path):
    entry = _write_fake(
        tmp_path,
        'process.stderr.write("boom\\n"); process.exit(2);\n',
    )
    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        runtime_entry=entry,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "PI_RUNTIME_UNAVAILABLE"
    assert result["error"]["stage"] == "spawn"
    assert result["error"]["retryable"] is False
    assert "boom" in result["error"]["message"]


def test_accepted_then_nonzero_exit(tmp_path):
    child = (
        'process.stdout.write(JSON.stringify({protocol:"om-pi-ipc.v1",type:"run.accepted",'
        'request_id:"req_1",run_id:"run_1",seq:1,payload:{runtime:"pi-agent-core",'
        'runtime_version:"0.84.2",session_id:null}})+"\\n", () => process.exit(2));\n'
    )
    entry = _write_fake(tmp_path, child)
    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        runtime_entry=entry,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "PI_PROCESS_EXITED"
    assert result["error"]["stage"] == "process"
    assert result["error"]["retryable"] is True


def test_timeout(tmp_path):
    entry = _write_fake(tmp_path, "setInterval(() => {}, 1000);\n")
    payload = _start_payload(
        model={
            "provider": "deepseek",
            "api_kind": "openai-completions",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "timeout_seconds": 1,
            "context_window_tokens": 24000,
            "max_output_tokens": 2048,
            "max_attempts": 2,
        },
        limits={
            "timeout_seconds": 1,
            "max_iterations": 16,
            "max_tool_calls": 12,
            "max_context_tokens": 24000,
            "max_consecutive_failed_tool_batches": 2,
            "final_answer_reserve_seconds": 20,
        },
    )
    result = run_pi_agent(
        payload,
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=1,
        runtime_entry=entry,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "PI_PROCESS_TIMEOUT"
    assert result["error"]["stage"] == "deadline"


def test_cooperative_cancel_before_spawn(tmp_path):
    entry = _write_fake(tmp_path, _HAPPY_CHILD)
    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        is_cancelled=lambda: True,
        runtime_entry=entry,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "CANCELLED"


def test_invalid_start_payload_rejected():
    result = run_pi_agent(
        _start_payload(session_id="s1"),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIG_ERROR"


def test_timeout_mismatch_rejected():
    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=99,  # limits.timeout_seconds is 60
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIG_ERROR"
    assert result["error"]["stage"] == "config"


def test_missing_node_runtime():
    entry = Path("/nonexistent/fake.mjs")
    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        runtime_entry=entry,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "PI_RUNTIME_UNAVAILABLE"


def test_runtime_command_honors_injected_environ_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_node = bin_dir / "node"
    fake_node.write_text("#!/bin/sh\necho v22.19.0\n", encoding="utf-8")
    fake_node.chmod(0o755)
    entry = tmp_path / "fake.mjs"
    entry.write_text("", encoding="utf-8")

    command, resolved_entry = _runtime_command(entry, environ={"PATH": str(bin_dir)})

    assert command == [str(fake_node), str(entry)]
    assert resolved_entry == entry


_TRACE_HEAD = """
import { createInterface } from "node:readline";
const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
const rec = (type, seq, payload) =>
  process.stdout.write(JSON.stringify({
    protocol: "om-pi-ipc.v1", type, request_id: "req_1", run_id: "run_1",
    seq, payload,
  }) + "\\n");
let n = 0;
rl.on("line", (line) => {
  n += 1;
  if (n === 1) {
    rec("run.accepted", 1, { runtime: "pi-agent-core", runtime_version: "0.84.2", session_id: null });
    rec("agent.event", 2, { event_type: "agent_start", data: {} });
    rec("agent.event", 3, { event_type: "turn_start", data: {} });
    rec("agent.event", 4, { event_type: "model_turn_completed", data: { stop_reason: "stop", usage: {} } });
    rec("agent.event", 5, { event_type: "turn_end", data: { stop_reason: "stop", usage: {} } });
    rec("agent.event", 6, { event_type: "agent_end", data: {} });
    rec("run.proposed", 7, { status: "answered", text: "hello", control_request: null, termination_reason: "stop", usage: {} });
  } else if (n === 2) {
    rec("run.final", 8, { status: "answered", text: "hello", control_request: null, termination_reason: "stop", usage: {}, committed: true });
"""


def test_oversized_line_fails_protocol(tmp_path):
    entry = _write_fake(tmp_path, 'process.stdout.write("x".repeat(1100000) + "\\n");\n')
    result = run_pi_agent(
        _start_payload(), request_id="req_1", run_id="run_1", timeout_seconds=60, runtime_entry=entry
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "PROTOCOL_ERROR"
    assert result["error"]["stage"] == "protocol"


def test_record_after_terminal_fails_protocol(tmp_path):
    child = _TRACE_HEAD + """
    rec("run.final", 9, { status: "answered", text: "hello", control_request: null, termination_reason: "stop", usage: {}, committed: true });
  }
});
"""
    entry = _write_fake(tmp_path, child)
    result = run_pi_agent(
        _start_payload(), request_id="req_1", run_id="run_1", timeout_seconds=60,
        on_proposed=lambda p: "commit", runtime_entry=entry,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "PROTOCOL_ERROR"
    assert result["error"]["stage"] == "protocol"


def test_terminal_then_hang_preserves_validated_result(tmp_path):
    child = _TRACE_HEAD + """
    setInterval(() => {}, 1000);
  }
});
"""
    entry = _write_fake(tmp_path, child)
    result = run_pi_agent(
        _start_payload(), request_id="req_1", run_id="run_1", timeout_seconds=60,
        on_proposed=lambda p: "commit", runtime_entry=entry,
    )
    assert result["ok"] is True
    assert result["result"]["status"] == "answered"
    assert result["result"]["text"] == "hello"


_CANCEL_RACE_CHILD = """
import { createInterface } from "node:readline";
const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
const rec = (type, seq, payload) =>
  process.stdout.write(JSON.stringify({
    protocol: "om-pi-ipc.v1", type, request_id: "req_1", run_id: "run_1",
    seq, payload,
  }) + "\\n");
let n = 0;
rl.on("line", (line) => {
  n += 1;
  if (n === 1) {
    rec("run.accepted", 1, { runtime: "pi-agent-core", runtime_version: "0.84.2", session_id: null });
    rec("agent.event", 2, { event_type: "agent_start", data: {} });
    rec("agent.event", 3, { event_type: "turn_start", data: {} });
    rec("agent.event", 4, { event_type: "model_turn_completed", data: { stop_reason: "stop", usage: {} } });
    rec("agent.event", 5, { event_type: "turn_end", data: { stop_reason: "stop", usage: {} } });
    rec("agent.event", 6, { event_type: "agent_end", data: {} });
    rec("run.proposed", 7, { status: "answered", text: "hello", control_request: null, termination_reason: "stop", usage: {} });
  } else if (n === 2) {
    // Python already sent run.cancel before reading the proposal; the child
    // answers with a cancelled final, not an answered commit.
    rec("run.final", 8, { status: "cancelled", text: "", control_request: null, termination_reason: "aborted", usage: {}, committed: false });
    process.exit(0);
  }
});
"""


def test_cancel_beats_fast_proposal(tmp_path):
    entry = _write_fake(tmp_path, _CANCEL_RACE_CHILD)
    calls = {"n": 0}

    def is_cancelled():
        calls["n"] += 1
        # False for the pre-spawn check, True on the first loop iteration so
        # the host cancellation is written before the buffered proposal.
        return calls["n"] > 1

    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        is_cancelled=is_cancelled,
        on_proposed=lambda p: "commit",
        runtime_entry=entry,
    )
    assert result["ok"] is True
    assert result["result"]["status"] == "cancelled"
    assert result["result"]["committed"] is False


def test_real_runtime_exits_promptly_on_commit():
    import time

    t0 = time.monotonic()
    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_proposed=lambda p: "commit",
    )
    elapsed = time.monotonic() - t0
    assert result["ok"] is True
    assert result["result"]["committed"] is True
    # Before the stdin-destroy fix the child hung for the fixed 1s grace +
    # SIGTERM. After the fix it exits cleanly well under that window.
    assert elapsed < 0.8


def test_empty_fixture_returns_model_error():
    result = run_pi_agent(
        _start_payload(debug={"fixture_response": "", "delay_ms": 0}),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "MODEL_ERROR"
    assert result["error"]["stage"] == "model"
    assert result["error"]["retryable"] is False


def test_real_tool_bridge_round_trip_and_sanitized_events():
    calls = []
    events = []

    def call_tool(payload):
        calls.append(payload)
        return {"ok": True, "summary": {"status": "ready"}}

    result = run_pi_agent(
        _tool_payload([_tool_turn(arguments={"index": 1}), {"text": "done"}]),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_event=events.append,
        on_tool_call=call_tool,
        on_proposed=lambda _payload: "commit",
    )

    assert result["ok"] is True
    assert result["result"]["text"] == "done"
    assert result["result"]["committed"] is True
    assert calls == [
        {
            "call_id": "call_1",
            "tool_name": "runtime_status",
            "arguments": {"index": 1},
        }
    ]
    tool_events = [
        event for event in events if event["event_type"].startswith("tool_execution_")
    ]
    assert tool_events == [
        {
            "event_type": "tool_execution_start",
            "data": {"call_id": "call_1", "tool_name": "runtime_status"},
        },
        {
            "event_type": "tool_execution_end",
            "data": {"call_id": "call_1", "tool_name": "runtime_status", "ok": True},
        },
    ]
    assert "arguments" not in json.dumps(events)
    assert "ready" not in json.dumps(events)


def test_pi_schema_rejects_invalid_arguments_before_callback():
    calls = []
    events = []
    result = run_pi_agent(
        _tool_payload([_tool_turn(arguments={"index": "bad"}), {"text": "recovered"}]),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_event=events.append,
        on_tool_call=lambda payload: calls.append(payload) or {"ok": True},
        on_proposed=lambda _payload: "commit",
    )

    assert result["ok"] is True
    assert result["result"]["text"] == "recovered"
    assert calls == []
    assert any(
        event["event_type"] == "tool_execution_end" and event["data"]["ok"] is False
        for event in events
    )


def test_multiple_tool_calls_run_in_source_order_without_overlap():
    trace = []
    active = False

    def call_tool(payload):
        nonlocal active
        assert active is False
        active = True
        trace.append(("start", payload["call_id"]))
        time.sleep(0.02)
        trace.append(("end", payload["call_id"]))
        active = False
        return {"ok": True, "index": payload["arguments"]["index"]}

    first = _tool_turn("call_1", arguments={"index": 1})["tool_calls"][0]
    second = _tool_turn("call_2", arguments={"index": 2})["tool_calls"][0]
    result = run_pi_agent(
        _tool_payload([{"tool_calls": [first, second]}, {"text": "done"}]),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_tool_call=call_tool,
        on_proposed=lambda _payload: "commit",
    )

    assert result["ok"] is True
    assert trace == [
        ("start", "call_1"),
        ("end", "call_1"),
        ("start", "call_2"),
        ("end", "call_2"),
    ]


def test_compact_failed_observation_becomes_error_tool_result():
    events = []
    result = run_pi_agent(
        _tool_payload([_tool_turn(), {"text": "fixed"}]),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_event=events.append,
        on_tool_call=lambda _payload: {
            "ok": False,
            "error": {"code": "INVALID_ARGUMENT", "message": "bad input"},
        },
        on_proposed=lambda _payload: "commit",
    )

    assert result["ok"] is True
    assert result["result"]["text"] == "fixed"
    assert any(
        event["event_type"] == "tool_execution_end" and event["data"]["ok"] is False
        for event in events
    )


def test_tool_callback_exception_is_terminal_and_redacted():
    def call_tool(_payload):
        raise RuntimeError("secret /private/account.json")

    result = run_pi_agent(
        _tool_payload([_tool_turn(), {"text": "must not commit"}]),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_tool_call=call_tool,
        on_proposed=lambda _payload: "commit",
    )

    assert result["ok"] is False
    assert result["error"] == {
        "code": "TOOL_BRIDGE_ERROR",
        "stage": "tool",
        "message": "tool callback failed",
        "retryable": False,
    }
    assert "secret" not in json.dumps(result)


def test_non_json_callback_result_fails_closed():
    result = run_pi_agent(
        _tool_payload([_tool_turn(), {"text": "must not commit"}]),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_tool_call=lambda _payload: {"ok": True, "value": object()},
        on_proposed=lambda _payload: "commit",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "TOOL_BRIDGE_ERROR"
    assert result["error"]["stage"] == "tool"


def test_python_rejects_tool_outside_host_allowlist(tmp_path):
    calls = []
    entry = _write_fake(
        tmp_path,
        _fake_tool_call_child(
            [
                {
                    "call_id": "call_1",
                    "tool_name": "symbol_config_update",
                    "arguments": {},
                }
            ]
        ),
    )
    result = run_pi_agent(
        _tool_payload([_tool_turn(), {"text": "unused"}]),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        runtime_entry=entry,
        on_tool_call=lambda payload: calls.append(payload) or {"ok": True},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "TOOL_BRIDGE_ERROR"
    assert result["error"]["retryable"] is False
    assert calls == []


def test_python_rejects_second_outstanding_tool_call(tmp_path):
    release = threading.Event()
    calls = []
    entry = _write_fake(
        tmp_path,
        _fake_tool_call_child(
            [
                {"call_id": "call_1", "tool_name": "runtime_status", "arguments": {}},
                {"call_id": "call_2", "tool_name": "runtime_status", "arguments": {}},
            ]
        ),
    )

    def call_tool(payload):
        calls.append(payload)
        release.wait(2)
        return {"ok": True}

    try:
        result = run_pi_agent(
            _tool_payload([_tool_turn(), {"text": "unused"}]),
            request_id="req_1",
            run_id="run_1",
            timeout_seconds=60,
            runtime_entry=entry,
            on_tool_call=call_tool,
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "PROTOCOL_ERROR"
        assert calls == [
            {"call_id": "call_1", "tool_name": "runtime_status", "arguments": {}}
        ]
    finally:
        release.set()
        assert _wait_for_tool_slot(False)


@pytest.mark.parametrize(
    "tool_result",
    [
        {"call_id": "other", "tool_name": "runtime_status", "observation": {"ok": True}},
        {"call_id": "call_1", "tool_name": "other", "observation": {"ok": True}},
    ],
)
def test_node_rejects_mismatched_tool_result(tool_result):
    record = _node_protocol_case([("tool.result", tool_result)])

    assert record["type"] == "run.error"
    assert record["payload"]["code"] == "PROTOCOL_ERROR"


def test_node_rejects_duplicate_tool_result():
    tool_result = {
        "call_id": "call_1",
        "tool_name": "runtime_status",
        "observation": {"ok": True},
    }
    record = _node_protocol_case(
        [("tool.result", tool_result), ("tool.result", tool_result)]
    )

    assert record["type"] == "run.error"
    assert record["payload"]["code"] == "PROTOCOL_ERROR"


def test_node_rejects_tool_result_after_cancel():
    record = _node_protocol_case(
        [
            ("run.cancel", {"reason": "host_cancel_requested"}),
            (
                "tool.result",
                {
                    "call_id": "call_1",
                    "tool_name": "runtime_status",
                    "observation": {"ok": True},
                },
            ),
        ]
    )

    assert record["type"] == "run.error"
    assert record["payload"]["code"] == "PROTOCOL_ERROR"


def test_cancelled_tool_keeps_single_worker_slot_until_callback_returns():
    entered = threading.Event()
    release = threading.Event()
    second_calls = []
    payload = _tool_payload([_tool_turn(), {"text": "unused"}])
    payload["limits"]["timeout_seconds"] = 5
    payload["limits"]["final_answer_reserve_seconds"] = 1
    payload["model"]["timeout_seconds"] = 5

    def slow_tool(_payload):
        entered.set()
        release.wait()
        return {"ok": True}

    try:
        cancelled = run_pi_agent(
            payload,
            request_id="req_1",
            run_id="run_1",
            timeout_seconds=5,
            on_tool_call=slow_tool,
            is_cancelled=entered.is_set,
        )
        assert cancelled["ok"] is True
        assert cancelled["result"]["status"] == "cancelled"
        assert _wait_for_tool_slot(True)

        for index in (2, 3):
            blocked = run_pi_agent(
                payload,
                request_id=f"req_{index}",
                run_id=f"run_{index}",
                timeout_seconds=5,
                on_tool_call=lambda call: second_calls.append(call) or {"ok": True},
            )
            assert blocked["ok"] is False
            assert blocked["error"] == {
                "code": "TOOL_BRIDGE_ERROR",
                "stage": "tool",
                "message": "another tool call is outstanding",
                "retryable": True,
            }
        assert second_calls == []
    finally:
        release.set()
        assert _wait_for_tool_slot(False)


def test_timeout_discards_late_tool_value_but_worker_owns_slot():
    entered = threading.Event()
    release = threading.Event()
    payload = _tool_payload([_tool_turn(), {"text": "unused"}])
    payload["limits"]["timeout_seconds"] = 1
    payload["limits"]["final_answer_reserve_seconds"] = 1
    payload["model"]["timeout_seconds"] = 1

    def slow_tool(_payload):
        entered.set()
        release.wait()
        return {"ok": True, "late": True}

    try:
        result = run_pi_agent(
            payload,
            request_id="req_1",
            run_id="run_1",
            timeout_seconds=1,
            on_tool_call=slow_tool,
        )
        assert entered.is_set()
        assert result["ok"] is False
        assert result["error"]["code"] == "PI_PROCESS_TIMEOUT"
        assert _wait_for_tool_slot(True)
    finally:
        release.set()
        assert _wait_for_tool_slot(False)


@pytest.mark.parametrize(
    ("limit_name", "observation"),
    [
        ("max_iterations", {"ok": True}),
        ("max_tool_calls", {"ok": True}),
        ("max_consecutive_failed_tool_batches", {"ok": False}),
        ("final_answer_reserve_seconds", {"ok": True}),
    ],
)
def test_budget_limit_allows_one_tool_free_final_turn(limit_name, observation):
    payload = _tool_payload([_tool_turn(), {"text": "forced final"}])
    payload["limits"][limit_name] = (
        payload["limits"]["timeout_seconds"]
        if limit_name == "final_answer_reserve_seconds"
        else 1
    )
    calls = []
    result = run_pi_agent(
        payload,
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_tool_call=lambda call: calls.append(call) or observation,
        on_proposed=lambda _payload: "commit",
    )

    assert result["ok"] is True
    assert result["result"]["text"] == "forced final"
    assert len(calls) == 1


def test_budget_exhaustion_without_text_is_not_a_successful_answer():
    payload = _tool_payload([_tool_turn(), _tool_turn("call_2")])
    payload["limits"]["max_iterations"] = 1
    calls = []
    result = run_pi_agent(
        payload,
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_tool_call=lambda call: calls.append(call) or {"ok": True},
        on_proposed=lambda _payload: "commit",
    )

    assert result["ok"] is False
    assert result["error"] == {
        "code": "BUDGET_EXHAUSTED",
        "stage": "budget",
        "message": "agent budget exhausted without a final answer",
        "retryable": False,
    }
    assert len(calls) == 1
