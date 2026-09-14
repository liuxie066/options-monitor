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
from src.application.bot.model_config import ModelSettings
from src.infrastructure.pi_agent_process import derive_pi_local_session_id, run_pi_migration_bridge
from src.infrastructure.private_storage import atomic_write_private_text, ensure_private_directory


_TEST_MODEL = ModelSettings(
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


def run_contract(contract, *, model_runner=None, **kwargs):
    if model_runner is not None:
        kwargs.setdefault("model_settings", _TEST_MODEL)
        sequence = 0
        def request(**values):
            nonlocal sequence
            sequence += 1
            turn = model_runner(ModelRequest(messages=tuple(values["messages"]), tools=tuple(values["tools"]),
                                force_finish=not values["tools"], timeout_seconds=values["timeout"], is_cancelled=kwargs.get("is_cancelled")))
            calls = [{"id": f"{sequence}:{c.call_id}", "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}} for c in turn.tool_calls]
            return {"message": {"role": "assistant", "content": turn.text, "tool_calls": calls},
                    "finish_reason": turn.finish_reason or ("tool_calls" if calls else "stop"), "usage": turn.usage}
        kwargs["model_request"] = request
    return _run_contract(contract, **kwargs)


@dataclass(frozen=True)
class ActualPiMigrationFixture:
    database: Path
    session_id: str
    source_runtime: Path
    target_runtime: Path


@lru_cache(maxsize=1)
def actual_pi_runtime_dirs() -> tuple[Path, Path]:
    source_path = os.environ.get("OM_PI_LEGACY_RUNTIME")
    target_path = os.environ.get("OM_PI_TARGET_RUNTIME")
    if not source_path or not target_path:
        raise ValueError("Offline conversion tests require prepared OM_PI_LEGACY_RUNTIME and OM_PI_TARGET_RUNTIME; no packages are installed by tests")
    source, target = Path(source_path), Path(target_path)
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
    "run_contract",
    "seed_actual_legacy_pi_store",
]
