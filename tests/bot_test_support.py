"""Offline Python Host model-turn fixtures; no runtime process or provider calls."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.application.bot.host import run_contract as _run_contract
from src.application.bot.model_config import ModelSettings


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


__all__ = ["_TEST_MODEL", "ModelRequest", "ModelRunner", "ModelTurn", "ToolCall", "run_contract"]
