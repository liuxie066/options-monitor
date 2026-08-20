from __future__ import annotations

import json
import os
import select
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.infrastructure.pi_agent_process import (  # noqa: E402
    _runtime_command,
    derive_pi_local_session_id,
    derive_pi_session_id,
    run_pi_agent,
)
from src.infrastructure import pi_agent_process as pi_process  # noqa: E402
from src.application.copilot.model_config import PiModelSettings  # noqa: E402


CONTINUATION_PROMPT_FOR_TEST = (
    "Continue exactly where the previous answer stopped. Do not repeat earlier text. "
    "Return only the continuation."
)


def _sse(events: list[dict]) -> bytes:
    return "".join(
        f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
        for event in events
    ).encode()


def _chat_response(
    *,
    text: str = "",
    finish_reason: str = "stop",
    usage: tuple[int, int] = (3, 2),
    tool_call: bool = False,
) -> bytes:
    delta: dict = {"role": "assistant"}
    if text:
        delta["content"] = text
    if tool_call:
        delta["tool_calls"] = [{
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "runtime_status", "arguments": "{\"index\":1}"},
        }]
    return _sse([
        {
            "id": "chatcmpl_test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "om-test",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        },
        {
            "id": "chatcmpl_test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "om-test",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": usage[0],
                "completion_tokens": usage[1],
                "total_tokens": sum(usage),
            },
        },
    ]) + b"data: [DONE]\n\n"


def _responses_response(
    *,
    text: str = "",
    status: str = "completed",
    usage: tuple[int, int] = (3, 2),
    tool_call: bool = False,
) -> bytes:
    if tool_call:
        item = {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "runtime_status",
            "arguments": "{\"index\":1}",
        }
        events = [
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "delta": "{\"index\":1}",
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        ]
    else:
        item = {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        events = [
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_text.delta", "output_index": 0, "delta": text},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        ]
    response = {
        "id": "resp_1",
        "object": "response",
        "status": status,
        "output": [item],
        "usage": {
            "input_tokens": usage[0],
            "output_tokens": usage[1],
            "total_tokens": sum(usage),
        },
    }
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    events.append({
        "type": "response.completed" if status == "completed" else "response.incomplete",
        "response": response,
    })
    return _sse(events)


@contextmanager
def _loopback_server(responses: list[dict]):
    requests: list[dict] = []
    scripted = list(responses)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except Exception:
                payload = None
            requests.append({
                "path": self.path,
                "payload": payload,
                "has_authorization": bool(self.headers.get("Authorization")),
            })
            response = scripted.pop(0) if scripted else {
                "status": 500,
                "body": b'{"error":{"message":"unscripted"}}',
                "content_type": "application/json",
            }
            started = response.get("started")
            if started is not None:
                started.set()
            delay = float(response.get("delay", 0))
            if delay:
                time.sleep(delay)
            body = response.get("body", b"")
            if isinstance(body, str):
                body = body.encode()
            try:
                self.send_response(int(response.get("status", 200)))
                self.send_header("Content-Type", response.get("content_type", "text/event-stream"))
                for name, value in response.get("headers", {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()


def _provider_payload(provider: str, base_url: str, **overrides) -> dict:
    api_kind = "openai-responses" if provider == "openai" else "openai-completions"
    payload = _start_payload(
        execution_environment="local",
        debug=None,
        model={
            "provider": provider,
            "api_kind": api_kind,
            "model": "om-test",
            "base_url": base_url,
            "timeout_seconds": 3,
            "context_window_tokens": 24_000,
            "max_output_tokens": 512,
            "max_attempts": 2,
        },
        limits={
            **_start_payload()["limits"],
            "timeout_seconds": 6,
            "final_answer_reserve_seconds": 1,
        },
    )
    payload.update(overrides)
    return payload


def _provider_env(*, keyed: bool = True, database: Path | None = None) -> dict[str, str]:
    environ = dict(os.environ)
    if keyed:
        environ["OM_PI_MODEL_API_KEY"] = "s4-loopback-secret"
    else:
        environ.pop("OM_PI_MODEL_API_KEY", None)
    if database is not None:
        environ["OM_PI_SESSION_DB"] = str(database)
    return environ


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


def _session_env(database: Path) -> dict[str, str]:
    environ = dict(os.environ)
    environ["OM_PI_SESSION_DB"] = str(database)
    return environ


def _run_session(
    database: Path,
    session_id: str,
    payload: dict,
    *,
    decision: str = "commit",
    run_id: str = "run_session",
    **kwargs,
):
    return run_pi_agent(
        payload,
        request_id=f"req_{run_id}",
        run_id=run_id,
        timeout_seconds=payload["limits"]["timeout_seconds"],
        on_proposed=lambda _payload: decision,
        environ=_session_env(database),
        **kwargs,
    )


def _session_entries(database: Path, session_id: str) -> list[dict]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT seq, id, parent_id, type, payload FROM entries "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    return [
        {
            "seq": seq,
            "id": entry_id,
            "parent_id": parent_id,
            "type": type_,
            "payload": json.loads(payload),
        }
        for seq, entry_id, parent_id, type_, payload in rows
    ]


def _set_latest_assistant_usage(
    database: Path, session_id: str, total_tokens: int
) -> None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT id, payload FROM entries "
            "WHERE session_id = ? AND type = 'message' ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        assert row is not None
        entry_id, encoded = row
        payload = json.loads(encoded)
        assert payload["message"]["role"] == "assistant"
        payload["message"]["usage"] = {
            "input": total_tokens,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": total_tokens,
            "cost": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "total": 0,
            },
        }
        connection.execute(
            "UPDATE entries SET payload = ? WHERE session_id = ? AND id = ?",
            (json.dumps(payload), session_id, entry_id),
        )


def _read_child_record(process, buffer: bytearray, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("timed out waiting for Node record")
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready:
            raise AssertionError("timed out waiting for Node record")
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            stderr = process.stderr.read().decode(errors="replace")
            raise AssertionError(f"Node exited before expected record: {stderr}")
        buffer.extend(chunk)
    line, _, remainder = buffer.partition(b"\n")
    buffer[:] = remainder
    return json.loads(line)


def _write_child_record(process, identity, seq, type_, payload):
    record = {
        "protocol": "om-pi-ipc.v1",
        "type": type_,
        **identity,
        "seq": seq,
        "payload": payload,
    }
    process.stdin.write((json.dumps(record) + "\n").encode())
    process.stdin.flush()


def _start_node_until_proposed(database, payload, run_id):
    command, entry = _runtime_command(None, None)
    identity = {"request_id": f"req_{run_id}", "run_id": run_id}
    process = subprocess.Popen(
        command,
        cwd=entry.parent.parent,
        env=_session_env(database),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    _write_child_record(process, identity, 1, "run.start", payload)
    buffer = bytearray()
    next_seq = 2
    try:
        while True:
            record = _read_child_record(process, buffer)
            if record["type"] == "tool.call":
                call = record["payload"]
                _write_child_record(
                    process,
                    identity,
                    next_seq,
                    "tool.result",
                    {
                        "call_id": call["call_id"],
                        "tool_name": call["tool_name"],
                        "observation": {"ok": True, "source": run_id},
                    },
                )
                next_seq += 1
            elif record["type"] == "run.proposed":
                return process, identity, next_seq
            elif record["type"] == "run.error":
                process.wait(timeout=2)
                stderr = process.stderr.read().decode(errors="replace")
                raise AssertionError((record, stderr))
    except Exception:
        process.kill()
        process.wait(timeout=2)
        raise


def _lease_expiration(database: Path, session_id: str) -> int:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT expires_at_ms FROM writer_leases WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    assert row is not None
    return row[0]


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


def _node_start_rejection(payload: dict) -> tuple[int, str]:
    command, entry = _runtime_command(None, None)
    start = {
        "protocol": "om-pi-ipc.v1",
        "type": "run.start",
        "request_id": "req_invalid_start",
        "run_id": "run_invalid_start",
        "seq": 1,
        "payload": payload,
    }
    completed = subprocess.run(
        command,
        cwd=entry.parent.parent,
        input=(json.dumps(start) + "\n").encode(),
        capture_output=True,
        timeout=5,
        check=False,
    )
    return completed.returncode, completed.stderr.decode(errors="replace")


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


def _fake_protocol_child(records, *, final_after_decision=None) -> str:
    scripted = [
        {"type": type_, "payload": payload} for type_, payload in records
    ]
    return f"""
import {{ createInterface }} from "node:readline";
const rl = createInterface({{ input: process.stdin, crlfDelay: Infinity }});
const rec = (type, seq, payload) =>
  process.stdout.write(JSON.stringify({{
    protocol: "om-pi-ipc.v1", type, request_id: "req_1", run_id: "run_1",
    seq, payload,
  }}) + "\\n");
const scripted = {json.dumps(scripted)};
const finalPayload = {json.dumps(final_after_decision)};
let n = 0;
rl.on("line", (line) => {{
  n += 1;
  if (n === 1) {{
    rec("run.accepted", 1, {{ runtime: "pi-agent-core", runtime_version: "0.84.2", session_id: null }});
    scripted.forEach((record, index) => rec(record.type, index + 2, record.payload));
  }} else if (n === 2 && finalPayload !== null) {{
    const committed = JSON.parse(line).type === "run.commit";
    rec("run.final", scripted.length + 2, {{ ...finalPayload, committed }});
    process.exit(0);
  }}
}});
"""


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
    rec("agent.event", 4, { event_type: "model_turn_completed", data: { stop_reason: "stop", attempt_count: 0, model_retry_count: 0, usage: { input: 1, output: 1, totalTokens: 2 }, usage_total: { input: 1, output: 1, totalTokens: 2 } } });
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


def test_pi_model_settings_is_exact_and_process_payload_is_secret_free():
    settings = PiModelSettings.from_config(
        {
            "provider": "openai",
            "model": "gpt-test",
            "base_url": "",
            "api_key_env": "PRIVATE_TEST_ENV",
            "timeout_seconds": 90,
            "context_window_tokens": 24_000,
            "max_output_tokens": 2_048,
            "max_attempts": 2,
        }
    )

    assert tuple(PiModelSettings.__dataclass_fields__) == (
        "provider",
        "api_kind",
        "model",
        "base_url",
        "api_key_env",
        "credential_name",
        "timeout_seconds",
        "context_window_tokens",
        "max_output_tokens",
        "max_attempts",
    )
    assert settings.api_kind == "openai-responses"
    assert settings.base_url == "https://api.openai.com/v1"
    assert settings.api_key_env == "PRIVATE_TEST_ENV"
    assert settings.credential_name
    assert settings.process_payload() == {
        "provider": "openai",
        "api_kind": "openai-responses",
        "model": "gpt-test",
        "base_url": "https://api.openai.com/v1",
        "timeout_seconds": 90,
        "context_window_tokens": 24_000,
        "max_output_tokens": 2_048,
        "max_attempts": 2,
    }
    assert "PRIVATE_TEST_ENV" not in json.dumps(settings.process_payload())
    assert settings.credential_name not in json.dumps(settings.process_payload())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("context_window_tokens", None),
        ("context_window_tokens", "24000"),
        ("context_window_tokens", 4_095),
        ("timeout_seconds", True),
        ("timeout_seconds", 121),
        ("max_output_tokens", 63),
        ("max_output_tokens", 4_097),
        ("max_attempts", 0),
        ("max_attempts", 4),
    ],
)
def test_pi_model_settings_rejects_invalid_numbers_instead_of_clamping(field, value):
    raw = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "context_window_tokens": 24_000,
    }
    if value is None:
        raw.pop(field, None)
    else:
        raw[field] = value

    with pytest.raises(ValueError):
        PiModelSettings.from_config(raw)


def test_pi_model_settings_rejects_unsafe_context_output_relation():
    with pytest.raises(ValueError, match="must exceed max_output_tokens"):
        PiModelSettings.from_config(
            {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "context_window_tokens": 4_096,
                "max_output_tokens": 2_096,
            }
        )


@pytest.mark.parametrize(
    "case",
    [
        "missing_context",
        "string_context",
        "timeout_high",
        "output_low",
        "attempts_high",
        "provider_api_mismatch",
        "invalid_base_url",
        "local_debug",
        "channel_debug",
        "eval_without_fixture",
    ],
)
def test_python_and_node_reject_closed_start_contract_before_provider(case):
    payload = _start_payload()
    if case == "missing_context":
        payload["model"].pop("context_window_tokens")
    elif case == "string_context":
        payload["model"]["context_window_tokens"] = "24000"
    elif case == "timeout_high":
        payload["model"]["timeout_seconds"] = 121
    elif case == "output_low":
        payload["model"]["max_output_tokens"] = 63
    elif case == "attempts_high":
        payload["model"]["max_attempts"] = 4
    elif case == "provider_api_mismatch":
        payload["model"].update(
            {"provider": "openai", "api_kind": "openai-completions"}
        )
    elif case == "invalid_base_url":
        payload["model"]["base_url"] = "file:///tmp/provider"
    elif case in {"local_debug", "channel_debug"}:
        payload["execution_environment"] = case.removesuffix("_debug")
    elif case == "eval_without_fixture":
        payload["debug"] = None
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(case)

    python_result = run_pi_agent(
        payload,
        request_id=f"req_{case}",
        run_id=f"run_{case}",
        timeout_seconds=60,
    )
    assert python_result["ok"] is False
    assert python_result["error"]["code"] == "CONFIG_ERROR"

    returncode, stderr = _node_start_rejection(payload)
    assert returncode == 2
    assert stderr.startswith("diagnostic: ")
    assert "s4-loopback-secret" not in stderr


@pytest.mark.parametrize(
    ("model_context", "scene_context", "message_chars", "expected_ok"),
    [
        (8_000, 12_000, 24_000, False),
        (12_000, 12_000, 24_000, True),
        (24_000, 12_000, 36_000, False),
    ],
)
def test_effective_context_budget_is_minimum_of_model_and_scene(
    model_context, scene_context, message_chars, expected_ok
):
    payload = _start_payload(user_message="x" * message_chars)
    payload["model"]["context_window_tokens"] = model_context
    payload["limits"]["max_context_tokens"] = scene_context

    result = run_pi_agent(
        payload,
        request_id=f"req_context_{model_context}_{scene_context}",
        run_id=f"run_context_{model_context}_{scene_context}",
        timeout_seconds=60,
        on_proposed=lambda _proposal: "commit",
    )

    assert result["ok"] is expected_ok, result
    if not expected_ok:
        assert result["error"] == {
            "code": "CONFIG_ERROR",
            "stage": "config",
            "message": "configured context budget is too small",
            "retryable": False,
        }


@pytest.mark.parametrize(
    "debug",
    [
        {"fixture_response": "hello"},
        {"fixture_turns": [{"text": "hello"}]},
    ],
)
def test_missing_fixture_delay_is_rejected(debug):
    result = run_pi_agent(
        _start_payload(debug=debug),
        request_id="req_missing_delay",
        run_id="run_missing_delay",
        timeout_seconds=60,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIG_ERROR"
    assert result["error"]["stage"] == "config"


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
    rec("agent.event", 4, { event_type: "model_turn_completed", data: { stop_reason: "stop", attempt_count: 0, model_retry_count: 0, usage: {}, usage_total: {} } });
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


def test_duplicate_proposal_fails_protocol(tmp_path):
    child = """
import { createInterface } from "node:readline";
const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
const rec = (type, seq, payload) =>
  process.stdout.write(JSON.stringify({
    protocol: "om-pi-ipc.v1", type, request_id: "req_1", run_id: "run_1",
    seq, payload,
  }) + "\\n");
rl.once("line", () => {
  rec("run.accepted", 1, { runtime: "pi-agent-core", runtime_version: "0.84.2", session_id: null });
  const proposal = { status: "answered", text: "hello", control_request: null, termination_reason: "stop", usage: {} };
  rec("run.proposed", 2, proposal);
  rec("run.proposed", 3, proposal);
  setInterval(() => {}, 1000);
});
"""
    entry = _write_fake(tmp_path, child)
    proposals = 0

    def admit(_proposal):
        nonlocal proposals
        proposals += 1
        return "commit"

    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_proposed=admit,
        runtime_entry=entry,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "PROTOCOL_ERROR"
    assert proposals == 1


def test_proposal_before_run_accepted_fails_protocol(tmp_path):
    entry = _write_fake(
        tmp_path,
        """
process.stdout.write(JSON.stringify({
  protocol: "om-pi-ipc.v1",
  type: "run.proposed",
  request_id: "req_1",
  run_id: "run_1",
  seq: 1,
  payload: { status: "answered", text: "hello", control_request: null, termination_reason: "stop", usage: {} },
}) + "\\n");
setInterval(() => {}, 1000);
""",
    )
    proposals = 0

    def admit(_proposal):
        nonlocal proposals
        proposals += 1
        return "commit"

    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_proposed=admit,
        runtime_entry=entry,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "PROTOCOL_ERROR"
    assert proposals == 0


@pytest.mark.parametrize(
    ("record_type", "record_payload"),
    [
        (
            "run.proposed",
            {
                "status": "answered",
                "text": "unearned",
                "control_request": None,
                "termination_reason": "stop",
                "usage": {},
            },
        ),
        (
            "run.final",
            {
                "status": "answered",
                "text": "unearned",
                "control_request": None,
                "termination_reason": "stop",
                "usage": {},
                "committed": False,
            },
        ),
        (
            "run.error",
            {
                "code": "MODEL_ERROR",
                "stage": "model",
                "message": "failed",
                "retryable": False,
            },
        ),
    ],
)
def test_result_record_while_tool_callback_is_outstanding_fails_protocol(
    tmp_path, record_type, record_payload
):
    final = {
        "status": "answered",
        "text": "unearned",
        "control_request": None,
        "termination_reason": "stop",
        "usage": {},
    }
    entry = _write_fake(
        tmp_path,
        _fake_protocol_child(
            [
                (
                    "tool.call",
                    {
                        "call_id": "call_1",
                        "tool_name": "runtime_status",
                        "arguments": {},
                    },
                ),
                (record_type, record_payload),
            ],
            final_after_decision=final if record_type == "run.proposed" else None,
        ),
    )
    release = threading.Event()

    def slow_tool(_payload):
        release.wait(timeout=5)
        return {"ok": True}

    try:
        result = run_pi_agent(
            _start_payload(tools=[_READ_TOOL]),
            request_id="req_1",
            run_id="run_1",
            timeout_seconds=60,
            on_tool_call=slow_tool,
            on_proposed=lambda _proposal: "commit",
            runtime_entry=entry,
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "PROTOCOL_ERROR"
    finally:
        release.set()
        assert _wait_for_tool_slot(False)


def test_tool_call_after_proposal_fails_protocol(tmp_path):
    proposal = {
        "status": "answered",
        "text": "hello",
        "control_request": None,
        "termination_reason": "stop",
        "usage": {},
    }
    entry = _write_fake(
        tmp_path,
        _fake_protocol_child(
            [
                ("run.proposed", proposal),
                (
                    "tool.call",
                    {
                        "call_id": "call_1",
                        "tool_name": "runtime_status",
                        "arguments": {},
                    },
                ),
            ],
            final_after_decision=proposal,
        ),
    )
    calls = []

    result = run_pi_agent(
        _start_payload(tools=[_READ_TOOL]),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_tool_call=lambda payload: calls.append(payload) or {"ok": True},
        on_proposed=lambda _proposal: "commit",
        runtime_entry=entry,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "PROTOCOL_ERROR"
    assert calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", "different-session-answer"),
        ("termination_reason", "length"),
        ("usage", {"input": 999}),
    ],
)
def test_final_answer_must_match_proposed_candidate(tmp_path, field, value):
    proposal = {
        "status": "answered",
        "text": "hello",
        "control_request": None,
        "termination_reason": "stop",
        "usage": {},
    }
    final = dict(proposal)
    final[field] = value
    entry = _write_fake(
        tmp_path,
        _fake_protocol_child(
            [("run.proposed", proposal)], final_after_decision=final
        ),
    )

    result = run_pi_agent(
        _start_payload(),
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_proposed=lambda _proposal: "commit",
        runtime_entry=entry,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "PROTOCOL_ERROR"


@pytest.mark.parametrize("decision", ["commit", "discard"])
def test_deadline_after_admission_does_not_send_cancel(
    tmp_path, monkeypatch, decision
):
    proposal = {
        "status": "answered",
        "text": "hello",
        "control_request": None,
        "termination_reason": "stop",
        "usage": {},
    }
    entry = _write_fake(
        tmp_path,
        _fake_protocol_child([("run.proposed", proposal)]),
    )
    outbound = []
    original_encode = pi_process._encode_envelope

    def capture_encode(type_, payload, identity, seq):
        outbound.append(type_)
        return original_encode(type_, payload, identity, seq)

    monkeypatch.setattr(pi_process, "_encode_envelope", capture_encode)
    payload = _start_payload()
    payload["model"]["timeout_seconds"] = 1
    payload["limits"]["timeout_seconds"] = 1
    payload["limits"]["final_answer_reserve_seconds"] = 1

    result = run_pi_agent(
        payload,
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=1,
        on_proposed=lambda _proposal: decision,
        runtime_entry=entry,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "PI_PROCESS_TIMEOUT"
    assert f"run.{decision}" in outbound
    assert "run.cancel" not in outbound


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
    rec("agent.event", 4, { event_type: "model_turn_completed", data: { stop_reason: "stop", attempt_count: 0, model_retry_count: 0, usage: {}, usage_total: {} } });
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
    ("limit_name", "observation", "reason"),
    [
        ("max_iterations", {"ok": True}, "model_turn_limit"),
        ("max_tool_calls", {"ok": True}, "tool_call_limit"),
        ("max_consecutive_failed_tool_batches", {"ok": False}, "tool_failure_limit"),
        ("final_answer_reserve_seconds", {"ok": True}, "time_reserve"),
    ],
)
def test_budget_limit_allows_one_tool_free_final_turn(limit_name, observation, reason):
    payload = _tool_payload([_tool_turn(), {"text": "forced final"}])
    payload["limits"][limit_name] = (
        payload["limits"]["timeout_seconds"]
        if limit_name == "final_answer_reserve_seconds"
        else 1
    )
    calls = []
    events = []
    result = run_pi_agent(
        payload,
        request_id="req_1",
        run_id="run_1",
        timeout_seconds=60,
        on_tool_call=lambda call: calls.append(call) or observation,
        on_event=events.append,
        on_proposed=lambda _payload: "commit",
    )

    assert result["ok"] is True
    assert result["result"]["text"] == "forced final"
    assert len(calls) == 1
    assert {
        "event_type": "forced_final_activated",
        "data": {"reason": reason},
    } in events


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


def test_session_id_is_sender_and_authority_scoped():
    first = derive_pi_session_id("feishu", "sender-a", "group-1", "key:us")

    assert first == derive_pi_session_id("feishu", "sender-a", "group-1", "key:us")
    assert first.startswith("om_") and len(first) == 67
    assert first != derive_pi_session_id("feishu", "sender-b", "group-1", "key:us")
    assert first != derive_pi_session_id("feishu", "sender-a", "group-1", "key:hk")
    assert first != derive_pi_session_id(
        "feishu", "sender-a", "group-1", "path:" + "a" * 64
    )
    with pytest.raises(ValueError):
        derive_pi_session_id("feishu", "", "group-1", "key:us")
    with pytest.raises(ValueError):
        derive_pi_session_id("feishu\0other", "sender-a", "group-1", "key:us")


def test_local_session_id_is_key_and_authority_scoped():
    first = derive_pi_local_session_id("key:us", "cli:portfolio")

    assert first == derive_pi_local_session_id("key:us", "cli:portfolio")
    assert first.startswith("om_") and len(first) == 67
    assert first != derive_pi_local_session_id("key:hk", "cli:portfolio")
    assert first != derive_pi_local_session_id("key:us", "cli:other")
    with pytest.raises(ValueError):
        derive_pi_local_session_id("", "cli:portfolio")


def test_persisted_session_continuity_and_scope_isolation(tmp_path):
    database = tmp_path / "pi_sessions.sqlite3"
    session_a = derive_pi_session_id("feishu", "sender-a", "group-1", "key:us")
    first_question = "unique-first-question"
    first_answer = "unique-first-answer"
    first_observation = "unique-first-tool-observation"
    first_payload = _tool_payload(
        [_tool_turn(arguments={"index": 1}), {"text": first_answer}],
        session_id=session_a,
        user_message=first_question,
    )

    first = _run_session(
        database,
        session_a,
        first_payload,
        run_id="turn_1",
        on_tool_call=lambda _call: {"ok": True, "value": first_observation},
    )
    second = _run_session(
        database,
        session_a,
        _start_payload(
            session_id=session_a,
            user_message="second-question",
            debug={
                "fixture_response": "second-answer",
                "delay_ms": 0,
                "expected_history": [
                    first_question,
                    first_observation,
                    first_answer,
                ],
            },
        ),
        run_id="turn_2",
    )

    assert first["ok"] is True and first["result"]["committed"] is True
    assert second["ok"] is True and second["result"]["committed"] is True

    isolated_ids = [
        derive_pi_session_id("feishu", "sender-b", "group-1", "key:us"),
        derive_pi_session_id("feishu", "sender-a", "group-1", "key:hk"),
        derive_pi_session_id(
            "feishu", "sender-a", "group-1", "path:" + "a" * 64
        ),
    ]
    for index, isolated_id in enumerate(isolated_ids):
        result = _run_session(
            database,
            isolated_id,
            _start_payload(
                session_id=isolated_id,
                user_message=f"isolated-{index}",
                debug={
                    "fixture_response": "isolated-answer",
                    "delay_ms": 0,
                    "forbidden_history": [
                        first_question,
                        first_observation,
                        first_answer,
                    ],
                },
            ),
            run_id=f"isolated_{index}",
        )
        assert result["ok"] is True


def test_persisted_session_survives_runtime_cwd_change(tmp_path):
    database = tmp_path / "pi_sessions.sqlite3"
    session_id = derive_pi_session_id("feishu", "sender-a", "group-1", "key:us")
    runtime = REPO / "agent-runtime" / "main.ts"
    entries = []
    for release in ("release-a", "release-b"):
        release_dir = tmp_path / release
        release_dir.mkdir()
        entry = release_dir / "main.ts"
        entry.symlink_to(runtime)
        entries.append(entry)

    first = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="before-upgrade",
            debug={"fixture_response": "before-upgrade-answer", "delay_ms": 0},
        ),
        run_id="before_upgrade",
        runtime_entry=entries[0],
    )
    second = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="after-upgrade",
            debug={
                "fixture_response": "after-upgrade-answer",
                "delay_ms": 0,
                "expected_history": ["before-upgrade", "before-upgrade-answer"],
            },
        ),
        run_id="after_upgrade",
        runtime_entry=entries[1],
    )

    assert first["ok"] is True and first["result"]["committed"] is True
    assert second["ok"] is True and second["result"]["committed"] is True


def test_transient_eval_run_does_not_create_session_database(tmp_path):
    database = tmp_path / "pi_sessions.sqlite3"
    result = run_pi_agent(
        _start_payload(),
        request_id="req_transient",
        run_id="run_transient",
        timeout_seconds=60,
        on_proposed=lambda _payload: "commit",
        environ=_session_env(database),
    )

    assert result["ok"] is True
    assert database.exists() is False


def test_only_admitted_turn_messages_are_persisted(tmp_path):
    database = tmp_path / "pi_sessions.sqlite3"
    session_id = derive_pi_session_id("feishu", "sender-a", "group-1", "key:us")
    runtime_secret = "ephemeral-control-snapshot"
    recovered_secret = "ephemeral-recovered-observation"
    committed = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="committed-question",
            runtime_context=[{"role": "system", "content": runtime_secret}],
            recovered_observations=[{"summary": recovered_secret}],
            debug={"fixture_response": "committed-answer", "delay_ms": 0},
        ),
        run_id="committed",
    )
    baseline = _session_entries(database, session_id)

    discarded = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="discarded-question",
            debug={"fixture_response": "discarded-answer", "delay_ms": 0},
        ),
        decision="discard",
        run_id="discarded",
    )

    started = time.monotonic()
    cancelled = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="cancelled-question",
            debug={"fixture_response": "cancelled-answer", "delay_ms": 5_000},
        ),
        run_id="cancelled",
        is_cancelled=lambda: time.monotonic() - started > 0.2,
    )
    failed = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="failed-question",
            debug={"fixture_response": "", "delay_ms": 0},
        ),
        run_id="failed",
    )
    after = _session_entries(database, session_id)
    persisted = json.dumps(after, ensure_ascii=False)

    assert committed["ok"] is True
    assert discarded["ok"] is True and discarded["result"]["committed"] is False
    assert cancelled["ok"] is True and cancelled["result"]["status"] == "cancelled"
    assert failed["ok"] is False and failed["error"]["code"] == "MODEL_ERROR"
    assert after == baseline
    for forbidden in (
        runtime_secret,
        recovered_secret,
        "discarded-question",
        "discarded-answer",
        "cancelled-question",
        "cancelled-answer",
        "failed-question",
    ):
        assert forbidden not in persisted


def test_killed_partial_turns_rewind_and_writer_lease_expires(tmp_path):
    cases = []
    for appended_messages in range(1, 5):
        database = tmp_path / f"partial_{appended_messages}.sqlite3"
        session_id = derive_pi_session_id(
            "feishu", f"sender-{appended_messages}", "group-1", "key:us"
        )
        baseline_question = f"baseline-question-{appended_messages}"
        baseline_answer = f"baseline-answer-{appended_messages}"
        assert _run_session(
            database,
            session_id,
            _start_payload(
                session_id=session_id,
                user_message=baseline_question,
                debug={"fixture_response": baseline_answer, "delay_ms": 0},
            ),
            run_id=f"baseline_{appended_messages}",
        )["ok"]
        baseline = _session_entries(database, session_id)
        baseline_marker = baseline[-1]["id"]
        partial_question = f"partial-question-{appended_messages}"
        partial_answer = f"partial-answer-{appended_messages}"
        payload = _tool_payload(
            [_tool_turn(arguments={"index": appended_messages}), {"text": partial_answer}],
            session_id=session_id,
            user_message=partial_question,
        )
        payload["debug"]["persist_delay_ms"] = 750
        process, identity, seq = _start_node_until_proposed(
            database, payload, f"partial_{appended_messages}"
        )
        _write_child_record(process, identity, seq, "run.commit", {})

        target = len(baseline) + appended_messages
        deadline = time.monotonic() + 10
        while len(_session_entries(database, session_id)) < target:
            if time.monotonic() >= deadline:
                process.kill()
                raise AssertionError(f"append point {appended_messages} was not reached")
            time.sleep(0.02)
        process.kill()
        process.wait(timeout=2)
        cases.append(
            (
                database,
                session_id,
                baseline_marker,
                baseline_question,
                baseline_answer,
                partial_question,
                partial_answer,
            )
        )

    compact_database = tmp_path / "compaction_crash.sqlite3"
    compact_session = derive_pi_session_id(
        "feishu", "compaction-crash", "group-1", "key:us"
    )
    old_question = "old-question-" + "q" * 16_000
    old_answer = "old-answer-" + "a" * 16_000
    assert _run_session(
        compact_database,
        compact_session,
        _start_payload(
            session_id=compact_session,
            user_message=old_question,
            debug={"fixture_response": old_answer, "delay_ms": 0},
        ),
        run_id="compact_seed",
    )["ok"]
    compact_before = _session_entries(compact_database, compact_session)
    crash_question = "crashed-after-compaction"
    crash_payload = _start_payload(
        session_id=compact_session,
        user_message=crash_question,
        model={
            **_start_payload()["model"],
            "context_window_tokens": 8_000,
        },
        limits={**_start_payload()["limits"], "max_context_tokens": 8_000},
        debug={
            "fixture_response": "uncommitted-after-compaction",
            "delay_ms": 0,
            "compaction_response": "compact-crash-summary",
        },
    )
    compact_process, _, _ = _start_node_until_proposed(
        compact_database, crash_payload, "compact_crash"
    )
    compact_process.kill()
    compact_process.wait(timeout=2)
    compact_after = _session_entries(compact_database, compact_session)

    assert [entry["type"] for entry in compact_after[len(compact_before) :]] == [
        "compaction",
        "custom",
    ]
    assert crash_question not in json.dumps(compact_after, ensure_ascii=False)
    busy = _run_session(
        compact_database,
        compact_session,
        _start_payload(
            session_id=compact_session,
            debug={"fixture_response": "busy", "delay_ms": 0},
        ),
        run_id="busy_before_ttl",
    )
    assert busy == {
        "ok": False,
        "error": {
            "code": "SESSION_ERROR",
            "stage": "session",
            "message": "session is temporarily busy",
            "retryable": True,
        },
    }

    expirations = [
        _lease_expiration(database, session_id)
        for database, session_id, *_ in cases
    ]
    expirations.append(_lease_expiration(compact_database, compact_session))
    time.sleep(max(0, max(expirations) / 1000 - time.time()) + 0.2)

    for (
        database,
        session_id,
        baseline_marker,
        baseline_question,
        baseline_answer,
        partial_question,
        partial_answer,
    ) in cases:
        recovered_question = f"recovery-question-{session_id[-4:]}"
        recovered = _run_session(
            database,
            session_id,
            _start_payload(
                session_id=session_id,
                user_message=recovered_question,
                debug={
                    "fixture_response": "recovered-answer",
                    "delay_ms": 0,
                    "expected_history": [baseline_question, baseline_answer],
                    "forbidden_history": [partial_question, partial_answer],
                },
            ),
            run_id=f"recovered_{session_id[-4:]}",
        )
        assert recovered["ok"] is True, recovered
        recovered_entry = next(
            entry
            for entry in _session_entries(database, session_id)
            if recovered_question in json.dumps(entry["payload"], ensure_ascii=False)
        )
        assert recovered_entry["parent_id"] == baseline_marker

    compact_recovered = _run_session(
        compact_database,
        compact_session,
        _start_payload(
            session_id=compact_session,
            user_message="after-compaction-crash",
            model={
                **_start_payload()["model"],
                "context_window_tokens": 8_000,
            },
            limits={**_start_payload()["limits"], "max_context_tokens": 8_000},
            debug={
                "fixture_response": "recovered-after-compaction",
                "delay_ms": 0,
                "expected_history": ["compact-crash-summary", old_answer],
                "forbidden_history": [old_question, crash_question],
            },
        ),
        run_id="compact_recovered",
    )
    assert compact_recovered["ok"] is True


@pytest.mark.parametrize("decision", ["discard", "cancel"])
def test_compaction_checkpoint_survives_current_turn_rejection(tmp_path, decision):
    database = tmp_path / f"compaction_{decision}.sqlite3"
    session_id = derive_pi_session_id(
        "feishu", f"sender-{decision}", "group-1", "key:us"
    )
    old_question = "checkpoint-old-question-" + "q" * 16_000
    old_answer = "checkpoint-old-answer-" + "a" * 16_000
    model = {**_start_payload()["model"], "context_window_tokens": 8_000}
    limits = {**_start_payload()["limits"], "max_context_tokens": 8_000}
    assert _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message=old_question,
            debug={"fixture_response": old_answer, "delay_ms": 0},
        ),
        run_id=f"seed_{decision}",
    )["ok"]
    before = _session_entries(database, session_id)
    rejected_question = f"rejected-{decision}-question"
    rejected = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message=rejected_question,
            model=model,
            limits=limits,
            debug={
                "fixture_response": f"rejected-{decision}-answer",
                "delay_ms": 0,
                "compaction_response": f"summary-{decision}",
                "expected_history": [f"summary-{decision}", old_answer],
                "forbidden_history": [old_question],
            },
        ),
        decision=decision,
        run_id=f"reject_{decision}",
    )
    after = _session_entries(database, session_id)

    assert rejected["ok"] is True, rejected
    assert rejected["result"]["committed"] is False
    assert [entry["type"] for entry in after[len(before) :]] == [
        "compaction",
        "custom",
    ]
    assert after[-1]["payload"]["data"]["kind"] == "compaction"
    assert rejected_question not in json.dumps(after, ensure_ascii=False)

    followup = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message=f"followup-{decision}",
            model=model,
            limits=limits,
            debug={
                "fixture_response": "followup-answer",
                "delay_ms": 0,
                "expected_history": [f"summary-{decision}", old_answer],
                "forbidden_history": [old_question, rejected_question],
            },
        ),
        run_id=f"followup_{decision}",
    )
    assert followup["ok"] is True


def test_compaction_persists_pi_payload_and_complete_tool_turn(tmp_path):
    database = tmp_path / "compaction_tool.sqlite3"
    session_id = derive_pi_session_id("feishu", "sender-a", "group-1", "key:us")
    old_question = "tool-old-question-" + "q" * 16_000
    old_answer = "tool-old-answer-" + "a" * 16_000
    model = {**_start_payload()["model"], "context_window_tokens": 8_000}
    limits = {**_start_payload()["limits"], "max_context_tokens": 8_000}
    assert _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message=old_question,
            debug={"fixture_response": old_answer, "delay_ms": 0},
        ),
        run_id="tool_seed",
    )["ok"]
    before = _session_entries(database, session_id)
    current_question = "current-tool-question"
    observation = {"ok": True, "value": "current-tool-observation"}
    payload = _tool_payload(
        [_tool_turn(arguments={"index": 7}), {"text": "current-tool-answer"}],
        session_id=session_id,
        user_message=current_question,
        model=model,
        limits=limits,
    )
    payload["debug"].update(
        {
            "compaction_response": "tool-compaction-summary",
            "expected_history": ["tool-compaction-summary", old_answer],
            "forbidden_history": [old_question],
        }
    )
    result = _run_session(
        database,
        session_id,
        payload,
        run_id="tool_compacted",
        on_tool_call=lambda _call: observation,
    )
    appended = _session_entries(database, session_id)[len(before) :]

    assert result["ok"] is True, result
    assert [entry["type"] for entry in appended] == [
        "compaction",
        "custom",
        "message",
        "message",
        "message",
        "message",
        "custom",
    ]
    compaction = appended[0]["payload"]
    assert "tool-compaction-summary" in compaction["summary"]
    assert old_answer in json.dumps(compaction["retainedTail"], ensure_ascii=False)
    assert compaction["tokensBefore"] > 0
    assert isinstance(compaction["usage"], dict)
    assert appended[1]["payload"]["data"]["kind"] == "compaction"
    roles = [entry["payload"]["message"]["role"] for entry in appended[2:6]]
    assert roles == ["user", "assistant", "toolResult", "assistant"]
    assert current_question in json.dumps(appended[2:], ensure_ascii=False)
    assert observation["value"] in json.dumps(appended[2:], ensure_ascii=False)
    assert appended[-1]["payload"]["data"]["kind"] == "turn"
    assert {entry["type"] for entry in _session_entries(database, session_id)} <= {
        "message",
        "compaction",
        "custom",
    }
    assert all(
        entry["payload"]["customType"] == "om.turn.commit.v1"
        for entry in _session_entries(database, session_id)
        if entry["type"] == "custom"
    )


def test_failed_compaction_keeps_previous_committed_branch(tmp_path):
    database = tmp_path / "failed_compaction.sqlite3"
    session_id = derive_pi_session_id("feishu", "sender-a", "group-1", "key:us")
    old_question = "failed-old-question-" + "q" * 16_000
    old_answer = "failed-old-answer-" + "a" * 16_000
    model = {**_start_payload()["model"], "context_window_tokens": 8_000}
    limits = {**_start_payload()["limits"], "max_context_tokens": 8_000}
    assert _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message=old_question,
            debug={"fixture_response": old_answer, "delay_ms": 0},
        ),
        run_id="failed_seed",
    )["ok"]
    before = _session_entries(database, session_id)

    failed = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="failed-current-question",
            model=model,
            limits=limits,
            debug={"fixture_response": "unused", "delay_ms": 0},
        ),
        run_id="failed_compaction",
    )
    assert failed == {
        "ok": False,
        "error": {
            "code": "SESSION_ERROR",
            "stage": "session",
            "message": "session context compaction failed",
            "retryable": False,
        },
    }
    assert _session_entries(database, session_id) == before

    recovered = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="recovered-current-question",
            model=model,
            limits=limits,
            debug={
                "fixture_response": "recovered-current-answer",
                "delay_ms": 0,
                "compaction_response": "recovered-summary",
                "expected_history": ["recovered-summary", old_answer],
                "forbidden_history": [old_question, "failed-current-question"],
            },
        ),
        run_id="recovered_compaction",
    )
    assert recovered["ok"] is True, recovered


@pytest.mark.parametrize(
    ("shape", "compaction_response"),
    [("boundary", ""), ("split", " \n\t")],
)
def test_blank_compaction_completion_keeps_previous_committed_branch(
    tmp_path, shape, compaction_response
):
    database = tmp_path / f"blank_compaction_{shape}.sqlite3"
    session_id = derive_pi_session_id(
        "feishu", f"blank-{shape}", "group-1", "key:us"
    )
    if shape == "boundary":
        committed_turns = [
            ("boundary-first-question-" + "q" * 6_000, "boundary-first-answer-" + "a" * 6_000),
            ("boundary-last-question-" + "q" * 6_000, "boundary-last-answer-" + "a" * 6_000),
        ]
    else:
        committed_turns = [
            ("split-question-" + "q" * 16_000, "split-answer-" + "a" * 16_000),
        ]
    for index, (question, answer) in enumerate(committed_turns):
        assert _run_session(
            database,
            session_id,
            _start_payload(
                session_id=session_id,
                user_message=question,
                debug={"fixture_response": answer, "delay_ms": 0},
            ),
            run_id=f"blank_{shape}_seed_{index}",
        )["ok"]
    before = _session_entries(database, session_id)
    model = {**_start_payload()["model"], "context_window_tokens": 8_000}
    limits = {**_start_payload()["limits"], "max_context_tokens": 8_000}

    failed = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message=f"blank-{shape}-current-question",
            model=model,
            limits=limits,
            debug={
                "fixture_response": "must-not-run",
                "delay_ms": 0,
                "compaction_response": compaction_response,
            },
        ),
        run_id=f"blank_{shape}_failed",
    )

    assert failed == {
        "ok": False,
        "error": {
            "code": "SESSION_ERROR",
            "stage": "session",
            "message": "session context compaction failed",
            "retryable": False,
        },
    }
    assert _session_entries(database, session_id) == before

    recovered = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message=f"blank-{shape}-recovery-question",
            debug={
                "fixture_response": "recovered-answer",
                "delay_ms": 0,
                "expected_history": [
                    item for turn in committed_turns for item in turn
                ],
                "forbidden_history": [f"blank-{shape}-current-question"],
            },
        ),
        run_id=f"blank_{shape}_recovered",
    )
    assert recovered["ok"] is True, recovered


def test_oversized_compaction_candidate_keeps_previous_committed_branch(tmp_path):
    database = tmp_path / "oversized_compaction.sqlite3"
    session_id = derive_pi_session_id(
        "feishu", "oversized-compaction", "group-1", "key:us"
    )
    old_question = "oversized-question"
    old_answer = "oversized-answer-" + "a" * 30_000
    assert _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message=old_question,
            debug={"fixture_response": old_answer, "delay_ms": 0},
        ),
        run_id="oversized_seed",
    )["ok"]
    before = _session_entries(database, session_id)
    model = {**_start_payload()["model"], "context_window_tokens": 8_000}
    limits = {**_start_payload()["limits"], "max_context_tokens": 8_000}

    failed = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="oversized-current-question",
            model=model,
            limits=limits,
            debug={
                "fixture_response": "must-not-run",
                "delay_ms": 0,
                "compaction_response": "oversized-summary",
            },
        ),
        run_id="oversized_failed",
    )

    assert failed == {
        "ok": False,
        "error": {
            "code": "SESSION_ERROR",
            "stage": "session",
            "message": "compacted context exceeds configured budget",
            "retryable": False,
        },
    }
    assert _session_entries(database, session_id) == before

    recovered = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="oversized-recovery-question",
            debug={
                "fixture_response": "oversized-recovery-answer",
                "delay_ms": 0,
                "expected_history": [old_question, old_answer],
                "forbidden_history": ["oversized-current-question"],
            },
        ),
        run_id="oversized_recovered",
    )
    assert recovered["ok"] is True, recovered


def test_compaction_uses_structural_tokens_after_provider_usage(tmp_path):
    database = tmp_path / "provider_usage_compaction.sqlite3"
    session_id = derive_pi_session_id("feishu", "provider-usage", "group-1", "key:us")
    old_question = "provider-usage-question-" + "q" * 16_000
    old_answer = "provider-usage-answer-" + "a" * 16_000
    assert _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message=old_question,
            debug={"fixture_response": old_answer, "delay_ms": 0},
        ),
        run_id="provider_usage_seed",
    )["ok"]
    _set_latest_assistant_usage(database, session_id, 7_000)

    model = {**_start_payload()["model"], "context_window_tokens": 8_000}
    limits = {**_start_payload()["limits"], "max_context_tokens": 8_000}
    compacted = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="provider-usage-current-question",
            model=model,
            limits=limits,
            debug={
                "fixture_response": "provider-usage-current-answer",
                "delay_ms": 0,
                "compaction_response": "provider-usage-summary",
                "expected_history": ["provider-usage-summary", old_answer],
                "forbidden_history": [old_question],
            },
        ),
        run_id="provider_usage_compacted",
    )
    assert compacted["ok"] is True, compacted
    compaction_count = sum(
        entry["type"] == "compaction"
        for entry in _session_entries(database, session_id)
    )

    followup = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="provider-usage-followup-question",
            model=model,
            limits=limits,
            debug={
                "fixture_response": "provider-usage-followup-answer",
                "delay_ms": 0,
                "expected_history": [
                    "provider-usage-summary",
                    old_answer,
                    "provider-usage-current-question",
                    "provider-usage-current-answer",
                ],
                "forbidden_history": [old_question],
            },
        ),
        run_id="provider_usage_followup",
    )

    assert followup["ok"] is True, followup
    assert sum(
        entry["type"] == "compaction"
        for entry in _session_entries(database, session_id)
    ) == compaction_count

    _set_latest_assistant_usage(database, session_id, 7_000)
    measured = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="provider-usage-measured-question",
            model=model,
            limits=limits,
            debug={
                "fixture_response": "provider-usage-measured-answer",
                "delay_ms": 0,
                "compaction_response": "provider-usage-measured-summary",
            },
        ),
        run_id="provider_usage_measured",
    )

    assert measured["ok"] is True, measured
    assert sum(
        entry["type"] == "compaction"
        for entry in _session_entries(database, session_id)
    ) == compaction_count + 1


def test_session_storage_errors_are_safe(tmp_path):
    session_id = derive_pi_session_id("feishu", "sender-a", "group-1", "key:us")
    payload = _start_payload(session_id=session_id)
    missing_env = dict(os.environ)
    missing_env.pop("OM_PI_SESSION_DB", None)
    missing = run_pi_agent(
        payload,
        request_id="req_missing_db",
        run_id="missing_db",
        timeout_seconds=60,
        environ=missing_env,
    )

    corrupt_database = tmp_path / "corrupt.sqlite3"
    corrupt_database.write_bytes(b"not sqlite")
    corrupt = _run_session(
        corrupt_database,
        session_id,
        payload,
        run_id="corrupt_db",
    )

    metadata_database = tmp_path / "metadata.sqlite3"
    assert _run_session(
        metadata_database,
        session_id,
        payload,
        run_id="metadata_seed",
    )["ok"]
    with sqlite3.connect(metadata_database) as connection:
        connection.execute(
            "UPDATE sessions SET metadata = ? WHERE id = ?",
            (json.dumps({"schema": "unknown"}), session_id),
        )
    metadata = _run_session(
        metadata_database,
        session_id,
        payload,
        run_id="bad_metadata",
    )

    for result in (missing, corrupt, metadata):
        assert result == {
            "ok": False,
            "error": {
                "code": "SESSION_ERROR",
                "stage": "session",
                "message": "session storage is unavailable",
                "retryable": False,
            },
        }
        assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize(
    ("provider", "base_suffix", "expected_path"),
    [
        ("openai", "", "/responses"),
        ("deepseek", "", "/chat/completions"),
        ("kimi", "/v1", "/v1/chat/completions"),
        ("kimi-code", "/coding/v1", "/coding/v1/chat/completions"),
        ("ollama", "/v1", "/v1/chat/completions"),
    ],
)
def test_loopback_provider_request_and_tool_round_trip(provider, base_suffix, expected_path):
    responder = _responses_response if provider == "openai" else _chat_response
    events: list[dict] = []
    with _loopback_server([
        {"body": responder(tool_call=True)},
        {"body": responder(text="provider answer", usage=(4, 3))},
    ]) as (root, requests):
        payload = _provider_payload(
            provider,
            root + base_suffix,
            tools=[_READ_TOOL],
        )
        result = run_pi_agent(
            payload,
            request_id=f"req_{provider}",
            run_id=f"run_{provider}",
            timeout_seconds=6,
            on_event=events.append,
            on_tool_call=lambda _call: {"ok": True, "status": "healthy"},
            on_proposed=lambda _proposal: "commit",
            environ=_provider_env(keyed=provider != "ollama"),
        )

    assert result["ok"] is True, result
    assert result["result"]["text"] == "provider answer"
    assert [request["path"] for request in requests] == [expected_path, expected_path]
    assert all(request["has_authorization"] for request in requests)
    captured = json.dumps(
        {"start": payload, "requests": requests, "events": events, "result": result}
    )
    assert "s4-loopback-secret" not in captured
    assert "ollama-local" not in captured
    assert "OM_PI_MODEL_API_KEY" not in captured
    first, second = (request["payload"] for request in requests)
    assert first["model"] == "om-test"
    if provider == "openai":
        assert first["max_output_tokens"] == 512
        assert first["temperature"] == 0
        assert first["input"][0]["role"] == "system"
        assert first["tools"][0]["type"] == "function"
        assert any(item.get("type") == "function_call" for item in second["input"])
        assert any(item.get("type") == "function_call_output" for item in second["input"])
    else:
        assert first.get("max_tokens", first.get("max_completion_tokens")) == 512
        assert first["messages"][0]["role"] == "system"
        assert first["tools"][0]["type"] == "function"
        assert any(message.get("tool_calls") for message in second["messages"])
        assert any(message.get("role") == "tool" for message in second["messages"])
        if provider in {"deepseek", "ollama"}:
            assert first["temperature"] == 0
        else:
            assert "temperature" not in first
        if provider == "deepseek":
            assert first["thinking"] == {"type": "disabled"}
        else:
            assert "thinking" not in first
    completed = [event["data"] for event in events if event["event_type"] == "model_turn_completed"]
    assert [event["attempt_count"] for event in completed] == [1, 1]
    assert [event["model_retry_count"] for event in completed] == [0, 0]
    assert completed[-1]["usage_total"]["totalTokens"] == 12
    assert result["result"]["usage"]["totalTokens"] == 12


def test_loopback_retry_success_and_attempt_counters():
    events: list[dict] = []
    with _loopback_server([
        {
            "status": 429,
            "body": '{"error":{"message":"rate limited private detail"}}',
            "content_type": "application/json",
            "headers": {"Retry-After": "0"},
        },
        {
            "status": 503,
            "body": '{"error":{"message":"temporary private detail"}}',
            "content_type": "application/json",
            "headers": {"Retry-After": "0"},
        },
        {"body": _chat_response(text="retried", usage=(5, 4))},
    ]) as (root, requests):
        payload = _provider_payload("deepseek", root)
        payload["model"]["max_attempts"] = 3
        result = run_pi_agent(
            payload,
            request_id="req_retry",
            run_id="run_retry",
            timeout_seconds=6,
            on_event=events.append,
            on_proposed=lambda _proposal: "commit",
            environ=_provider_env(),
        )

    assert result["ok"] is True, result
    assert len(requests) == 3
    completed = [event["data"] for event in events if event["event_type"] == "model_turn_completed"]
    assert completed == [{
        "stop_reason": "stop",
        "attempt_count": 3,
        "model_retry_count": 2,
        "usage": {"input": 5, "output": 4, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 9},
        "usage_total": {"input": 5, "output": 4, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 9},
    }]
    assert result["result"]["usage"]["totalTokens"] == 9


def test_loopback_missing_key_fails_before_http():
    events: list[dict] = []
    with _loopback_server([{"body": _chat_response(text="must not run")}]) as (root, requests):
        result = run_pi_agent(
            _provider_payload("deepseek", root),
            request_id="req_missing_key",
            run_id="run_missing_key",
            timeout_seconds=6,
            on_event=events.append,
            environ=_provider_env(keyed=False),
        )

    assert requests == []
    assert result == {
        "ok": False,
        "error": {
            "code": "MODEL_ERROR",
            "stage": "model",
            "message": "model authentication failed",
            "retryable": False,
        },
    }
    completed = [event["data"] for event in events if event["event_type"] == "model_turn_completed"]
    assert completed[0]["attempt_count"] == 0
    assert completed[0]["model_retry_count"] == 0


@pytest.mark.parametrize(
    ("responses", "max_attempts", "expected_message", "expected_attempts"),
    [
        (
            [{"status": 401, "body": '{"error":{"message":"secret auth body"}}', "content_type": "application/json"}],
            2,
            "model authentication failed",
            1,
        ),
        (
            [{"status": 429, "body": '{"error":{"message":"private rate body"}}', "content_type": "application/json", "headers": {"Retry-After": "0"}}] * 2,
            2,
            "model request failed",
            2,
        ),
        (
            [{"status": 200, "body": b"data: not-json\n\n"}],
            1,
            "model response was invalid",
            1,
        ),
    ],
)
def test_loopback_provider_failures_are_safe(
    responses, max_attempts, expected_message, expected_attempts
):
    events: list[dict] = []
    with _loopback_server(responses) as (root, requests):
        payload = _provider_payload("deepseek", root)
        payload["model"]["max_attempts"] = max_attempts
        result = run_pi_agent(
            payload,
            request_id="req_safe_failure",
            run_id="run_safe_failure",
            timeout_seconds=6,
            on_event=events.append,
            environ=_provider_env(),
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "MODEL_ERROR"
    assert result["error"]["message"] == expected_message
    assert len(requests) == expected_attempts
    encoded = json.dumps(result)
    assert "secret auth body" not in encoded
    assert "private rate body" not in encoded
    assert root not in encoded
    completed = [event["data"] for event in events if event["event_type"] == "model_turn_completed"]
    assert completed[0]["attempt_count"] == expected_attempts
    assert completed[0]["model_retry_count"] == max(expected_attempts - 1, 0)


def test_loopback_model_timeout_is_bounded():
    events: list[dict] = []
    with _loopback_server([{"delay": 1.5, "body": _chat_response(text="too late")}]) as (root, requests):
        payload = _provider_payload("deepseek", root)
        payload["model"].update({"timeout_seconds": 1, "max_attempts": 1})
        result = run_pi_agent(
            payload,
            request_id="req_timeout",
            run_id="run_timeout",
            timeout_seconds=6,
            on_event=events.append,
            environ=_provider_env(),
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "MODEL_ERROR"
    assert result["error"]["message"] == "model request failed"
    assert len(requests) == 1
    completed = [event["data"] for event in events if event["event_type"] == "model_turn_completed"]
    assert completed[0]["attempt_count"] == 1


def test_loopback_host_abort_cancels_inflight_request():
    started = threading.Event()
    events: list[dict] = []
    cancel_checks = 0

    def is_cancelled():
        nonlocal cancel_checks
        cancel_checks += 1
        return started.is_set()

    with _loopback_server([
        {"started": started, "delay": 2, "body": _chat_response(text="too late")},
    ]) as (root, requests):
        payload = _provider_payload("deepseek", root)
        payload["model"]["max_attempts"] = 1
        result = run_pi_agent(
            payload,
            request_id="req_abort",
            run_id="run_abort",
            timeout_seconds=6,
            on_event=events.append,
            is_cancelled=is_cancelled,
            environ=_provider_env(),
        )

    assert cancel_checks > 1
    assert len(requests) == 1
    assert result["ok"] is True, result
    assert result["result"]["status"] == "cancelled"
    assert result["result"]["termination_reason"] == "aborted"
    completed = [event["data"] for event in events if event["event_type"] == "model_turn_completed"]
    assert completed[0]["attempt_count"] == 1


@pytest.mark.parametrize(
    ("second_reason", "expected_reason"),
    [("stop", "stop"), ("length", "length")],
)
def test_loopback_length_continuation_is_canonical_and_tool_free(
    tmp_path, second_reason, expected_reason
):
    database = tmp_path / f"continuation_{second_reason}.sqlite3"
    session_id = derive_pi_session_id("feishu", second_reason, "group-1", "key:us")
    events: list[dict] = []
    with _loopback_server([
        {"body": _chat_response(text="first", finish_reason="length", usage=(3, 2))},
        {"body": _chat_response(text="second", finish_reason=second_reason, usage=(4, 3))},
    ]) as (root, requests):
        payload = _provider_payload(
            "deepseek",
            root,
            session_id=session_id,
            tools=[_READ_TOOL],
            user_message="continue this",
        )
        result = run_pi_agent(
            payload,
            request_id=f"req_continue_{second_reason}",
            run_id=f"run_continue_{second_reason}",
            timeout_seconds=6,
            on_event=events.append,
            on_proposed=lambda _proposal: "commit",
            environ=_provider_env(database=database),
        )

    assert result["ok"] is True, result
    assert result["result"]["text"] == "firstsecond"
    assert result["result"]["termination_reason"] == expected_reason
    assert result["result"]["usage"]["totalTokens"] == 12
    assert len(requests) == 2
    assert "tools" in requests[0]["payload"]
    assert "tools" not in requests[1]["payload"]
    assert CONTINUATION_PROMPT_FOR_TEST in json.dumps(requests[1]["payload"])
    completed = [event["data"] for event in events if event["event_type"] == "model_turn_completed"]
    assert [item["attempt_count"] for item in completed] == [1, 1]
    assert [item["usage_total"]["totalTokens"] for item in completed] == [5, 12]
    entries = _session_entries(database, session_id)
    messages = [entry["payload"]["message"] for entry in entries if entry["type"] == "message"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == [{"type": "text", "text": "firstsecond"}]
    assert messages[-1]["usage"]["totalTokens"] == 12
    assert CONTINUATION_PROMPT_FOR_TEST not in json.dumps(entries)


def test_loopback_length_not_continued_without_iteration_budget():
    with _loopback_server([
        {"body": _chat_response(text="partial", finish_reason="length")},
    ]) as (root, requests):
        payload = _provider_payload("deepseek", root)
        payload["limits"]["max_iterations"] = 1
        result = run_pi_agent(
            payload,
            request_id="req_no_continue",
            run_id="run_no_continue",
            timeout_seconds=6,
            on_proposed=lambda _proposal: "commit",
            environ=_provider_env(),
        )

    assert result["ok"] is True, result
    assert result["result"]["text"] == "partial"
    assert result["result"]["termination_reason"] == "length"
    assert len(requests) == 1


def test_loopback_failed_length_continuation_does_not_commit_partial(tmp_path):
    database = tmp_path / "failed_continuation.sqlite3"
    session_id = derive_pi_session_id("feishu", "failed", "group-1", "key:us")
    with _loopback_server([
        {"body": _chat_response(text="partial", finish_reason="length")},
        {
            "status": 500,
            "body": '{"error":{"message":"failed continuation private"}}',
            "content_type": "application/json",
        },
    ]) as (root, requests):
        payload = _provider_payload("deepseek", root, session_id=session_id)
        payload["model"]["max_attempts"] = 1
        result = run_pi_agent(
            payload,
            request_id="req_failed_continue",
            run_id="run_failed_continue",
            timeout_seconds=6,
            environ=_provider_env(database=database),
        )

    assert len(requests) == 2
    assert result["ok"] is False
    assert result["error"]["message"] == "model request failed"
    assert _session_entries(database, session_id) == []
    assert "partial" not in json.dumps(result)
    assert "failed continuation private" not in json.dumps(result)


def test_loopback_resumed_session_continuation_uses_only_canonical_history(tmp_path):
    database = tmp_path / "resumed_continuation.sqlite3"
    session_id = derive_pi_session_id("feishu", "resumed", "group-1", "key:us")
    seeded = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="seed question",
            debug={"fixture_response": "seed answer", "delay_ms": 0},
        ),
        run_id="seed_resume",
    )
    assert seeded["ok"] is True

    with _loopback_server([
        {"body": _chat_response(text="left", finish_reason="length")},
        {"body": _chat_response(text="right", finish_reason="stop")},
    ]) as (root, requests):
        payload = _provider_payload(
            "deepseek",
            root,
            session_id=session_id,
            user_message="resumed question",
        )
        result = run_pi_agent(
            payload,
            request_id="req_resumed_continue",
            run_id="run_resumed_continue",
            timeout_seconds=6,
            on_proposed=lambda _proposal: "commit",
            environ=_provider_env(database=database),
        )

    assert result["ok"] is True, result
    assert result["result"]["text"] == "leftright"
    first_request = json.dumps(requests[0]["payload"])
    assert "seed question" in first_request
    assert "seed answer" in first_request
    entries = _session_entries(database, session_id)
    encoded = json.dumps(entries)
    assert "seed question" in encoded
    assert "resumed question" in encoded
    assert "leftright" in encoded
    assert CONTINUATION_PROMPT_FOR_TEST not in encoded


def test_loopback_compaction_shares_policy_and_counts_usage_once(tmp_path):
    database = tmp_path / "provider_compaction.sqlite3"
    session_id = derive_pi_session_id("feishu", "provider-compaction", "group-1", "key:us")
    seeded = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="old question " + "q" * 16_000,
            debug={"fixture_response": "old answer " + "a" * 16_000, "delay_ms": 0},
        ),
        run_id="seed_provider_compaction",
    )
    assert seeded["ok"] is True
    events: list[dict] = []
    with _loopback_server([
        {
            "status": 429,
            "body": '{"error":{"message":"compact retry private"}}',
            "content_type": "application/json",
            "headers": {"Retry-After": "0"},
        },
        {"body": _chat_response(text="compact summary", usage=(7, 3))},
        {"body": _chat_response(text="current answer", usage=(5, 4))},
    ]) as (root, requests):
        payload = _provider_payload(
            "deepseek",
            root,
            session_id=session_id,
            user_message="current question",
        )
        payload["model"]["context_window_tokens"] = 8_000
        payload["limits"]["max_context_tokens"] = 8_000
        result = run_pi_agent(
            payload,
            request_id="req_provider_compaction",
            run_id="run_provider_compaction",
            timeout_seconds=6,
            on_event=events.append,
            on_proposed=lambda _proposal: "commit",
            environ=_provider_env(database=database),
        )

    assert result["ok"] is True, result
    assert len(requests) == 3
    assert [request["path"] for request in requests] == ["/chat/completions"] * 3
    completed = [event["data"] for event in events if event["event_type"] == "model_turn_completed"]
    assert completed == [{
        "stop_reason": "stop",
        "attempt_count": 1,
        "model_retry_count": 1,
        "usage": {"input": 5, "output": 4, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 9},
        "usage_total": {"input": 12, "output": 7, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 19},
    }]
    assert result["result"]["usage"]["totalTokens"] == 19
    entries = _session_entries(database, session_id)
    assert sum(entry["type"] == "compaction" for entry in entries) == 1
    assert "compact summary" in json.dumps(entries)


def test_loopback_split_compaction_drains_counters_before_retried_main(tmp_path):
    database = tmp_path / "split_provider_compaction.sqlite3"
    session_id = derive_pi_session_id(
        "feishu", "split-provider-compaction", "group-1", "key:us"
    )
    for index, (question, answer) in enumerate(
        [
            ("older question", "older answer"),
            (
                "latest question " + "q" * 16_000,
                "latest answer " + "a" * 16_000,
            ),
        ]
    ):
        seeded = _run_session(
            database,
            session_id,
            _start_payload(
                session_id=session_id,
                user_message=question,
                debug={"fixture_response": answer, "delay_ms": 0},
            ),
            run_id=f"seed_split_provider_compaction_{index}",
        )
        assert seeded["ok"] is True, seeded

    events: list[dict] = []
    with _loopback_server([
        {"body": _chat_response(text="history summary", usage=(2, 1))},
        {"body": _chat_response(text="turn prefix summary", usage=(3, 2))},
        {
            "status": 503,
            "body": '{"error":{"message":"main retry private"}}',
            "content_type": "application/json",
            "headers": {"Retry-After": "0"},
        },
        {"body": _chat_response(text="current answer", usage=(5, 4))},
    ]) as (root, requests):
        payload = _provider_payload(
            "deepseek",
            root,
            session_id=session_id,
            user_message="current question",
        )
        payload["model"]["context_window_tokens"] = 8_000
        payload["limits"]["max_context_tokens"] = 8_000
        result = run_pi_agent(
            payload,
            request_id="req_split_provider_compaction",
            run_id="run_split_provider_compaction",
            timeout_seconds=6,
            on_event=events.append,
            on_proposed=lambda _proposal: "commit",
            environ=_provider_env(database=database),
        )

    assert result["ok"] is True, result
    assert len(requests) == 4
    assert [request["path"] for request in requests] == ["/chat/completions"] * 4
    completed = [
        event["data"]
        for event in events
        if event["event_type"] == "model_turn_completed"
    ]
    assert completed == [{
        "stop_reason": "stop",
        "attempt_count": 2,
        "model_retry_count": 1,
        "usage": {
            "input": 5,
            "output": 4,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 9,
        },
        "usage_total": {
            "input": 10,
            "output": 7,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 17,
        },
    }]
    assert result["result"]["usage"] == completed[0]["usage_total"]
    entries = _session_entries(database, session_id)
    assert sum(entry["type"] == "compaction" for entry in entries) == 1


def test_loopback_cancelled_main_keeps_committed_compaction_usage(tmp_path):
    database = tmp_path / "cancel_after_provider_compaction.sqlite3"
    session_id = derive_pi_session_id(
        "feishu", "cancel-after-provider-compaction", "group-1", "key:us"
    )
    seeded = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="old question " + "q" * 16_000,
            debug={
                "fixture_response": "old answer " + "a" * 16_000,
                "delay_ms": 0,
            },
        ),
        run_id="seed_cancel_after_provider_compaction",
    )
    assert seeded["ok"] is True, seeded

    main_started = threading.Event()
    events: list[dict] = []
    with _loopback_server([
        {"body": _chat_response(text="compact summary", usage=(7, 3))},
        {
            "started": main_started,
            "delay": 2,
            "body": _chat_response(text="too late", usage=(5, 4)),
        },
    ]) as (root, requests):
        payload = _provider_payload(
            "deepseek",
            root,
            session_id=session_id,
            user_message="current question",
        )
        payload["model"]["context_window_tokens"] = 8_000
        payload["limits"]["max_context_tokens"] = 8_000
        result = run_pi_agent(
            payload,
            request_id="req_cancel_after_provider_compaction",
            run_id="run_cancel_after_provider_compaction",
            timeout_seconds=6,
            on_event=events.append,
            is_cancelled=main_started.is_set,
            environ=_provider_env(database=database),
        )

    committed_usage = {
        "input": 7,
        "output": 3,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 10,
    }
    assert len(requests) == 2
    assert result["ok"] is True, result
    assert result["result"]["status"] == "cancelled"
    assert result["result"]["usage"] == committed_usage
    completed = [
        event["data"]
        for event in events
        if event["event_type"] == "model_turn_completed"
    ]
    assert len(completed) == 1
    assert completed[0]["stop_reason"] == "aborted"
    assert completed[0]["attempt_count"] == 1
    assert completed[0]["usage_total"] == committed_usage
    assert any(
        event == {
            "event_type": "context_compaction_committed",
            "data": {"compaction_count": 1, "usage_total": committed_usage},
        }
        for event in events
    )
    entries = _session_entries(database, session_id)
    assert sum(entry["type"] == "compaction" for entry in entries) == 1
    assert "compact summary" in json.dumps(entries)


def test_loopback_cancelled_uncommitted_compaction_publishes_no_usage(tmp_path):
    database = tmp_path / "cancel_during_provider_compaction.sqlite3"
    session_id = derive_pi_session_id(
        "feishu", "cancel-during-provider-compaction", "group-1", "key:us"
    )
    seeded = _run_session(
        database,
        session_id,
        _start_payload(
            session_id=session_id,
            user_message="old question " + "q" * 16_000,
            debug={
                "fixture_response": "old answer " + "a" * 16_000,
                "delay_ms": 0,
            },
        ),
        run_id="seed_cancel_during_provider_compaction",
    )
    assert seeded["ok"] is True, seeded

    compaction_started = threading.Event()
    events: list[dict] = []
    with _loopback_server([
        {
            "started": compaction_started,
            "delay": 2,
            "body": _chat_response(text="uncommitted summary", usage=(7, 3)),
        },
    ]) as (root, requests):
        payload = _provider_payload(
            "deepseek",
            root,
            session_id=session_id,
            user_message="current question",
        )
        payload["model"]["context_window_tokens"] = 8_000
        payload["limits"]["max_context_tokens"] = 8_000
        result = run_pi_agent(
            payload,
            request_id="req_cancel_during_provider_compaction",
            run_id="run_cancel_during_provider_compaction",
            timeout_seconds=6,
            on_event=events.append,
            is_cancelled=compaction_started.is_set,
            environ=_provider_env(database=database),
        )

    assert len(requests) == 1
    assert result["ok"] is True, result
    assert result["result"]["status"] == "cancelled"
    assert result["result"]["usage"] == {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
    }
    assert all(event["event_type"] != "context_compaction_committed" for event in events)
    entries = _session_entries(database, session_id)
    assert sum(entry["type"] == "compaction" for entry in entries) == 0
    assert "uncommitted summary" not in json.dumps(entries)


def test_s5_application_call_path_uses_pi_without_legacy_fallback():
    host = (REPO / "src/application/copilot/host.py").read_text(encoding="utf-8")
    harness = (REPO / "src/application/copilot/local_harness.py").read_text(encoding="utf-8")

    assert "run_pi_agent(" in host
    assert "PiModelSettings" in host
    assert "def _resolve_pi_model(" in harness
    assert "PiModelSettings" in harness
    for source in (host, harness):
        assert "copilot.engine" not in source
        assert "copilot.model_client" not in source
        assert "copilot.conversation_memory" not in source
        assert "_resolve_model_runner" not in source
        assert "model_runner" not in source
        assert "run_engine(" not in source
