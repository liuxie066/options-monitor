from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.application.bot.control_handoff import CONTROL_PREVIEW_TOOL
from src.application.bot.host import run_contract as _run_contract
from src.application.bot.model_config import PiModelSettings
from src.infrastructure.pi_agent_process import derive_pi_local_session_id, run_pi_migration_bridge
from src.infrastructure.private_storage import atomic_write_private_text, ensure_private_directory


_TEST_MODEL = PiModelSettings(
    provider="ollama",
    api_kind="openai-completions",
    model="om-test",
    base_url="http://127.0.0.1:11434/v1",
    api_key_env="",
    credential_name="",
    timeout_seconds=90,
    context_window_tokens=24_000,
    max_output_tokens=2_048,
    max_attempts=1,
)


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    attempt_count: int = 1
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    force_finish: bool = False
    timeout_seconds: int | None = None
    is_cancelled: Callable[[], bool] | None = None
    iteration_id: str | None = None
    context_hash: str | None = None


ModelRunner = Callable[[ModelRequest], ModelTurn]


def fake_pi_agent(model_runner: ModelRunner):
    """Return a deterministic Pi-process boundary for Host unit tests."""

    def run(start_payload, *, on_event, on_tool_call, on_proposed, is_cancelled, **_kwargs):
        system_parts = [start_payload["system_prompt"]]
        if start_payload["runtime_context"]:
            system_parts.append(
                "<om-runtime-context>\n"
                + "\n\n".join(item["content"] for item in start_payload["runtime_context"])
                + "\n</om-runtime-context>"
            )
        if start_payload["recovered_observations"]:
            system_parts.append(
                "<om-recovered-observations>\n"
                + json.dumps(start_payload["recovered_observations"], ensure_ascii=False)
                + "\n</om-recovered-observations>"
            )
        messages = [
            {"role": "system", "content": "\n\n".join(system_parts)},
            {"role": "user", "content": start_payload["user_message"]},
        ]
        tools = tuple(dict(item) for item in start_payload["tools"])
        limits = start_payload["limits"]
        usage_total = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0}
        retry_count = 0
        answer_parts: list[str] = []
        tool_calls = 0
        observations = 0
        on_event({"event_type": "agent_start", "data": {}})

        def model_turn(*, force_finish: bool):
            nonlocal retry_count
            on_event({"event_type": "turn_start", "data": {}})
            try:
                turn = model_runner(
                    ModelRequest(
                        messages=tuple(dict(item) for item in messages),
                        tools=() if force_finish else tools,
                        force_finish=force_finish,
                        timeout_seconds=int(limits["timeout_seconds"]),
                        is_cancelled=is_cancelled,
                    )
                )
            except Exception as error:
                attempts = max(1, int(getattr(error, "attempt_count", 1) or 1))
                retry_count += attempts - 1
                on_event(
                    {
                        "event_type": "model_turn_completed",
                        "data": {
                            "stop_reason": "error",
                            "attempt_count": attempts,
                            "model_retry_count": retry_count,
                            "usage": {key: 0 for key in usage_total},
                            "usage_total": dict(usage_total),
                        },
                    }
                )
                return None
            usage = {
                "input": max(0, int(turn.usage.get("input_tokens") or 0)),
                "output": max(0, int(turn.usage.get("output_tokens") or 0)),
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": max(0, int(turn.usage.get("total_tokens") or 0)),
            }
            if not usage["totalTokens"]:
                usage["totalTokens"] = usage["input"] + usage["output"]
            for key, value in usage.items():
                usage_total[key] += value
            retry_count += max(0, int(turn.attempt_count or 1) - 1)
            on_event(
                {
                    "event_type": "model_turn_completed",
                    "data": {
                        "stop_reason": turn.finish_reason or ("toolUse" if turn.tool_calls else "stop"),
                        "attempt_count": max(1, int(turn.attempt_count or 1)),
                        "model_retry_count": retry_count,
                        "usage": usage,
                        "usage_total": dict(usage_total),
                    },
                }
            )
            return turn

        def finalize(
            text: str,
            *,
            status: str = "answered",
            control_request: dict | None = None,
            termination_reason: str = "stop",
        ):
            if (
                status == "answered"
                and start_payload.get("execution_environment") != "eval"
                and any(tool.get("name") == "submit_answer" for tool in tools)
            ):
                admitted = on_tool_call(
                    {
                        "call_id": "fake_submit_answer",
                        "tool_name": "submit_answer",
                        "arguments": {
                            "mode": "conceptual",
                            "status": "complete",
                            "answer_markdown": text,
                            "claims": [],
                        },
                    }
                )
                approved = admitted.get("approved_answer") if isinstance(admitted, dict) else None
                if isinstance(approved, dict) and isinstance(approved.get("text"), str):
                    text = approved["text"]
            on_event({"event_type": "agent_end", "data": {}})
            proposal = {
                "status": status,
                "text": text,
                "control_request": control_request,
                "termination_reason": termination_reason,
                "usage": dict(usage_total),
            }
            decision = on_proposed(proposal)
            if decision == "cancel":
                return {
                    "ok": True,
                    "result": {**proposal, "status": "cancelled", "text": "", "committed": False},
                }
            return {"ok": True, "result": {**proposal, "committed": decision == "commit"}}

        max_turns = max(1, int(limits["max_iterations"]))
        max_tools = max(1, int(limits["max_tool_calls"]))
        for _ in range(max_turns):
            if is_cancelled and is_cancelled():
                return {
                    "ok": True,
                    "result": {
                        "status": "cancelled",
                        "text": "",
                        "control_request": None,
                        "termination_reason": "cancelled",
                        "usage": dict(usage_total),
                        "committed": False,
                    },
                }
            turn = model_turn(force_finish=False)
            if turn is None:
                break
            if turn.text and not turn.tool_calls:
                answer_parts.append(turn.text)
                if turn.finish_reason == "length":
                    messages.append({"role": "assistant", "content": turn.text})
                    continue
                return finalize("".join(answer_parts))
            if not turn.tool_calls:
                continue
            messages.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [
                        {"id": call.call_id, "name": call.name, "arguments": dict(call.arguments)}
                        for call in turn.tool_calls
                    ],
                }
            )
            for call in turn.tool_calls:
                if tool_calls >= max_tools:
                    observation = {
                        "tool_name": call.name,
                        "ok": False,
                        "status": "failed",
                        "error": "BUDGET_EXHAUSTED",
                    }
                else:
                    tool_calls += 1
                    try:
                        callback_result = on_tool_call(
                            {
                                "call_id": call.call_id,
                                "tool_name": call.name,
                                "arguments": dict(call.arguments),
                            }
                        )
                        if call.name == CONTROL_PREVIEW_TOOL:
                            observation = callback_result["observation"]
                            control_request = callback_result["control_request"]
                        else:
                            observation = callback_result
                    except Exception:
                        return {
                            "ok": False,
                            "error": {
                                "code": "TOOL_BRIDGE_ERROR",
                                "stage": "tool",
                                "message": "tool callback failed",
                                "retryable": False,
                            },
                        }
                observations += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "name": call.name,
                        "content": json.dumps(observation, ensure_ascii=False, default=str),
                    }
                )
                if call.name == CONTROL_PREVIEW_TOOL and control_request is not None:
                    return finalize(
                        "",
                        status="control_requested",
                        control_request=control_request,
                        termination_reason="control_preview_requested",
                    )

        if observations:
            on_event(
                {
                    "event_type": "forced_final_activated",
                    "data": {"reason": "model_turn_limit"},
                }
            )
            turn = model_turn(force_finish=True)
            if turn is not None and turn.text:
                answer_parts.append(turn.text)
                return finalize("".join(answer_parts))
        return {
            "ok": False,
            "error": {
                "code": "MODEL_ERROR" if not observations else "BUDGET_EXHAUSTED",
                "stage": "model" if not observations else "budget",
                "message": "test model did not produce a final answer",
                "retryable": False,
            },
        }

    return run


def run_contract(contract, *, model_runner=None, **kwargs):
    if model_runner is None:
        return _run_contract(contract, **kwargs)
    kwargs.setdefault("model_settings", _TEST_MODEL)
    with patch("src.application.bot.host.run_pi_agent", fake_pi_agent(model_runner)):
        return _run_contract(contract, **kwargs)


@dataclass(frozen=True)
class ActualPiMigrationFixture:
    database: Path
    session_id: str
    source_runtime: Path
    target_runtime: Path


@lru_cache(maxsize=1)
def actual_pi_runtime_dirs() -> tuple[Path, Path]:
    target = Path(__file__).resolve().parents[1] / "agent-runtime"
    configured = os.environ.get("OM_PI_LEGACY_RUNTIME")
    if configured:
        source = Path(configured)
    else:
        # Keep the real legacy SDK isolated from the shipped runtime dependencies.
        temporary = tempfile.TemporaryDirectory(prefix="om-pi-legacy-test-")
        actual_pi_runtime_dirs.temporary = temporary
        source = Path(temporary.name)
        packages = (
            "@earendil-works/pi-agent-core", "@earendil-works/pi-ai",
            "@earendil-works/pi-session-backend-sqlite-node",
        )
        (source / "package.json").write_text(json.dumps({
            "private": True, "type": "module",
            "dependencies": {name: "0.84.2" for name in packages},
        }), encoding="utf-8")
        subprocess.run(
            ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=source, check=True, capture_output=True, text=True, timeout=180,
        )
    for runtime, version in ((source, "0.84.2"), (target, "0.85.1")):
        manifest = json.loads((runtime / "package.json").read_text(encoding="utf-8"))
        assert set(manifest["dependencies"].values()) == {version}
        assert (runtime / "package-lock.json").is_file()
        assert (runtime / "node_modules").is_dir()
    return source.resolve(), target.resolve()


def _migration_usage() -> dict[str, Any]:
    return {
        "input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


def seed_actual_legacy_pi_store(database: Path) -> ActualPiMigrationFixture:
    source_runtime, target_runtime = actual_pi_runtime_dirs()
    ensure_private_directory(database.parent)
    session_id = derive_pi_local_session_id("key:us", "pi-migration-fixture")
    user = {"role": "user", "content": "old question", "timestamp": 1_700_000_000_010}
    assistant = {
        "role": "assistant", "content": [{"type": "text", "text": "old answer"}],
        "api": "openai-responses", "provider": "openai", "model": "fixture",
        "usage": _migration_usage(), "stopReason": "stop", "timestamp": 1_700_000_000_020,
    }
    retained_user = {"role": "user", "content": "retained question", "timestamp": 1_699_999_999_000}
    retained_assistant = {**assistant, "content": [{"type": "text", "text": "retained answer"}],
                          "timestamp": 1_699_999_999_010}
    entries = [
        {"id": "old_user", "parentId": None, "timestamp": 1_700_000_000_010,
         "type": "message", "message": user},
        {"id": "old_assistant", "parentId": "old_user", "timestamp": 1_700_000_000_020,
         "type": "message", "message": assistant},
        {"id": "old_commit", "parentId": "old_assistant", "timestamp": 1_700_000_000_030,
         "type": "custom", "customType": "om.turn.commit.v1",
         "data": {"run_id": "old_run", "kind": "turn"}},
        {"id": "old_compaction", "parentId": "old_commit", "timestamp": 1_700_000_000_040,
         "type": "compaction", "summary": "old summary",
         "retainedTail": [retained_user, retained_assistant], "tokensBefore": 100,
         "usage": _migration_usage(), "fromHook": False},
        {"id": "old_compaction_commit", "parentId": "old_compaction", "timestamp": 1_700_000_000_050,
         "type": "custom", "customType": "om.turn.commit.v1",
         "data": {"run_id": "old_compaction_run", "kind": "compaction"}},
    ]
    canonical = {
        "format": "om-pi-export.v1",
        "sessions": [{
            "id": session_id, "createdAt": 1_700_000_000_000, "entries": entries,
            "excludedTailCount": 0, "contextSha256": "0" * 64, "contentSha256": "0" * 64,
        }],
    }
    export_path = database.with_name(database.name + ".seed.json")
    atomic_write_private_text(export_path, json.dumps(canonical, sort_keys=True))
    run_pi_migration_bridge(
        "import", source_runtime,
        {"expected-version": "0.84.2", "database": database, "input": export_path},
    )
    return ActualPiMigrationFixture(database, session_id, source_runtime, target_runtime)


__all__ = [
    "_TEST_MODEL",
    "ModelRequest",
    "ModelRunner",
    "ModelTurn",
    "ToolCall",
    "ActualPiMigrationFixture",
    "actual_pi_runtime_dirs",
    "fake_pi_agent",
    "run_contract",
    "seed_actual_legacy_pi_store",
]
