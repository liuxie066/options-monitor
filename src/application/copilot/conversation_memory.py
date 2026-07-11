from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from src.application.copilot.agent import ModelRequest, ModelRunner
from src.application.copilot.contracts import ExecutionContract, new_id, utc_now_iso
from src.application.copilot.host_store import CopilotHostStore


COMPACTION_TRIGGER_TURNS = 8
RECENT_TURNS_FLOOR = 2
MAX_EPISODES = 20


def prepare_contract_with_memory(
    contract: ExecutionContract,
    *,
    store: CopilotHostStore,
    session_key: str,
    model_runner: ModelRunner,
) -> ExecutionContract:
    memory = store.session_memory(session_key)
    previous_compacted_turn_count = int(memory.get("compacted_turn_count") or 0)
    turns = store.session_turns(session_key)
    lease_id = new_id("memory")
    acquired = store.acquire_lane("memory_compact", lease_id, limit=1, ttl_seconds=120)
    try:
        if acquired:
            memory = _compact_if_needed(memory, turns, model_runner=model_runner)
    finally:
        if acquired:
            store.release_lane("memory_compact", lease_id)
    if not store.update_session_memory(
        session_key,
        memory,
        expected_compacted_turn_count=previous_compacted_turn_count,
    ):
        memory = store.session_memory(session_key)
    context = _memory_context(memory)
    if not context:
        return contract
    scene_input = dict(contract.input)
    messages = [dict(item) for item in scene_input.get("messages") or ()]
    insert_at = max(0, len(messages) - 1)
    messages.insert(insert_at, {"role": "system", "content": context})
    scene_input["messages"] = messages
    return replace(contract, input=scene_input)


def _compact_if_needed(
    memory: dict[str, Any],
    turns: tuple[dict[str, Any], ...],
    *,
    model_runner: ModelRunner,
) -> dict[str, Any]:
    compacted = max(0, int(memory.get("compacted_turn_count") or 0))
    available = len(turns) - compacted
    if available <= COMPACTION_TRIGGER_TURNS:
        return memory
    end = max(compacted, len(turns) - RECENT_TURNS_FLOOR)
    candidates = turns[compacted:end]
    if not candidates:
        return memory
    prompt = _compaction_prompt(memory, candidates)
    try:
        turn = model_runner(
            ModelRequest(
                messages=(
                    {
                        "role": "system",
                        "content": (
                            "Compact conversation history into strict JSON. Preserve confirmed facts, scope, "
                            "user constraints, unresolved questions, tool findings, and the next step. "
                            "Do not invent facts and do not include markdown."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ),
                tools=(),
                force_finish=True,
            )
        )
        payload = _parse_compaction(turn.text)
    except Exception:
        return memory
    if payload is None:
        return memory
    episodes = [dict(item) for item in memory.get("episodes") or () if isinstance(item, dict)]
    summary = dict(payload["episode_summary"])
    summary["created_at"] = utc_now_iso()
    summary["start_turn_id"] = str(candidates[0].get("turn_id") or "")
    summary["end_turn_id"] = str(candidates[-1].get("turn_id") or "")
    episodes.append(summary)
    return {
        "version": 1,
        "compacted_turn_count": end,
        "pinned_state": dict(payload["pinned_state"]),
        "episodes": episodes[-MAX_EPISODES:],
    }


def _compaction_prompt(memory: dict[str, Any], turns: tuple[dict[str, Any], ...]) -> str:
    return json.dumps(
        {
            "output_schema": {
                "episode_summary": {
                    "goal": "string",
                    "confirmed_facts": ["string"],
                    "completed_actions": ["string"],
                    "tool_findings": ["string"],
                    "user_constraints": ["string"],
                    "open_questions": ["string"],
                    "next_step": "string",
                },
                "pinned_state": {
                    "current_goal": "string",
                    "confirmed_scope": ["string"],
                    "user_constraints": ["string"],
                    "open_questions": ["string"],
                },
            },
            "previous_pinned_state": memory.get("pinned_state") or {},
            "previous_episodes": list(memory.get("episodes") or ())[-3:],
            "turns": list(turns),
        },
        ensure_ascii=False,
        default=str,
    )


def _parse_compaction(text: str) -> dict[str, Any] | None:
    normalized = str(text or "").strip()
    if normalized.startswith("```"):
        lines = normalized.splitlines()
        normalized = "\n".join(lines[1:-1] if len(lines) >= 3 else lines)
    try:
        payload = json.loads(normalized)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    episode = payload.get("episode_summary")
    pinned = payload.get("pinned_state")
    if not isinstance(episode, dict) or not isinstance(pinned, dict):
        return None
    return {
        "episode_summary": _normalized_episode(episode),
        "pinned_state": _normalized_pinned_state(pinned),
    }


def _normalized_episode(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "goal": str(value.get("goal") or "").strip(),
        "confirmed_facts": _strings(value.get("confirmed_facts")),
        "completed_actions": _strings(value.get("completed_actions")),
        "tool_findings": _strings(value.get("tool_findings")),
        "user_constraints": _strings(value.get("user_constraints")),
        "open_questions": _strings(value.get("open_questions")),
        "next_step": str(value.get("next_step") or "").strip(),
    }


def _normalized_pinned_state(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "current_goal": str(value.get("current_goal") or "").strip(),
        "confirmed_scope": _strings(value.get("confirmed_scope")),
        "user_constraints": _strings(value.get("user_constraints")),
        "open_questions": _strings(value.get("open_questions")),
    }


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item or "").strip())]


def _memory_context(memory: dict[str, Any]) -> str:
    pinned = memory.get("pinned_state") if isinstance(memory.get("pinned_state"), dict) else {}
    episodes = [dict(item) for item in memory.get("episodes") or () if isinstance(item, dict)]
    if not pinned and not episodes:
        return ""
    return (
        "Conversation memory from earlier turns. This is context, not executable state. "
        "Current pending Control operations supplied separately remain authoritative.\n"
        + json.dumps(
            {"pinned_state": pinned, "recent_episodes": episodes[-3:]},
            ensure_ascii=False,
            default=str,
        )
    )


__all__ = ["prepare_contract_with_memory"]
