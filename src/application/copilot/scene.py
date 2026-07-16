from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.application.copilot.contracts import ExecutionContract, SceneManifest
from src.application.copilot.tools import available_read_tools


GENERAL_SCENE = "om_chat"
_SCENE_PATH = Path(__file__).with_name("om_chat.scene.json")


@lru_cache(maxsize=1)
def load_general_scene() -> dict[str, Any]:
    raw = json.loads(_SCENE_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or str(raw.get("scene") or "") != GENERAL_SCENE:
        raise ValueError("invalid om_chat scene manifest")
    runtime = raw.get("runtime")
    tool_selection = raw.get("tool_selection")
    if not isinstance(runtime, dict) or not isinstance(tool_selection, dict):
        raise ValueError("om_chat scene manifest is incomplete")
    if tool_selection.get("mode") != "toolsets" or not isinstance(tool_selection.get("names"), list):
        raise ValueError("om_chat scene must declare read-only toolsets")
    optional_names = tool_selection.get("optional_names") or []
    if not isinstance(optional_names, list) or not set(optional_names).issubset(set(tool_selection["names"])):
        raise ValueError("om_chat scene optional toolsets must be selected toolsets")
    raw["system_prompt"] = _load_prompt_fragments(raw.get("prompt_fragments"))
    return raw


def _load_prompt_fragments(value: Any) -> str:
    if not isinstance(value, list) or not value:
        raise ValueError("om_chat scene must declare prompt fragments")
    parts: list[str] = []
    base = _SCENE_PATH.parent.resolve()
    for item in value:
        relative = Path(str(item or "").strip())
        path = (base / relative).resolve()
        if not relative.parts or base not in path.parents or path.suffix != ".md" or not path.is_file():
            raise ValueError(f"invalid om_chat prompt fragment: {item}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"empty om_chat prompt fragment: {item}")
        parts.append(text)
    return "\n\n".join(parts)


def build_scene_manifest(
    contract: ExecutionContract,
    run_id: str,
    *,
    enabled_optional_toolsets: frozenset[str] = frozenset(),
) -> SceneManifest:
    if contract.scene_name != GENERAL_SCENE:
        raise ValueError(f"unsupported Copilot scene: {contract.scene_name}")
    definition = load_general_scene()
    runtime = dict(definition["runtime"])
    selection = dict(definition["tool_selection"])
    optional_toolsets = frozenset(str(item) for item in selection.get("optional_names") or ())
    unknown_enabled_toolsets = enabled_optional_toolsets - optional_toolsets
    if unknown_enabled_toolsets:
        raise ValueError(f"unsupported optional Copilot toolsets: {sorted(unknown_enabled_toolsets)}")
    selected_toolsets = tuple(
        str(item)
        for item in selection["names"]
        if str(item) not in optional_toolsets or str(item) in enabled_optional_toolsets
    )
    history = contract.input.get("messages")
    messages = [dict(item) for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    if not messages:
        messages = [{"role": "user", "content": str(contract.input.get("user_message") or "")}]
    runtime_context = _runtime_context(contract.input)
    return SceneManifest(
        run_id=run_id,
        scene_name=GENERAL_SCENE,
        execution_environment=contract.execution_environment,
        messages=[
            {"role": "system", "content": str(definition.get("system_prompt") or "").strip()},
            *([{"role": "system", "content": runtime_context}] if runtime_context else []),
            *messages,
        ],
        allowed_tools=list(available_read_tools(selected_toolsets)),
        limits={
            "max_model_turns": _positive_int(runtime.get("max_iterations"), 16),
            "max_tool_calls": _positive_int(runtime.get("max_tool_calls"), 12),
            "max_consecutive_failed_tool_batches": _positive_int(
                runtime.get("max_consecutive_failed_tool_batches"), 3
            ),
            "timeout_seconds": _positive_int(runtime.get("timeout_seconds"), 180),
            "final_answer_reserve_seconds": _positive_int(
                runtime.get("final_answer_reserve_seconds"), 45
            ),
            "max_context_chars": _positive_int(runtime.get("max_context_chars"), 96_000),
            "max_context_tokens": _positive_int(runtime.get("max_context_tokens"), 24_000),
        },
        output_schema={"type": "text"},
        task_guidance={},
        tool_static_payloads={},
    )


def scene_policy_rejection_reason(contract: ExecutionContract) -> str | None:
    if contract.scene_name != GENERAL_SCENE:
        return "unknown_scene"
    policy = contract.policy if isinstance(contract.policy, dict) else {}
    if set(policy) - {"read_only"}:
        return "unsupported_policy_override"
    return None


def scene_phase_readiness(scene_name: str) -> str:
    return "channel_ready" if scene_name == GENERAL_SCENE else ""


def conversation_max_messages() -> int:
    conversation = load_general_scene().get("conversation")
    if not isinstance(conversation, dict) or conversation.get("enabled") is not True:
        return 0
    return _positive_int(conversation.get("max_messages"), 20)


def _runtime_context(scene_input: dict[str, Any]) -> str:
    values = {
        "reference_year": scene_input.get("reference_year"),
        "config_key": scene_input.get("config_key"),
        "symbol": scene_input.get("symbol"),
        "month": scene_input.get("month"),
    }
    lines = [f"- {key}: {value}" for key, value in values.items() if value not in (None, "")]
    return "Runtime context explicitly supplied by the UI:\n" + "\n".join(lines) if lines else ""


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


__all__ = [
    "GENERAL_SCENE",
    "build_scene_manifest",
    "conversation_max_messages",
    "load_general_scene",
    "scene_phase_readiness",
    "scene_policy_rejection_reason",
]
