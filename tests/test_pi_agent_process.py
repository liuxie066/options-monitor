from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.infrastructure.pi_agent_process import (  # noqa: E402
    _runtime_command,
    run_pi_agent,
)


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
