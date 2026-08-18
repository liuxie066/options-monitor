from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from src.application.copilot.contracts import ExecutionContract
from src.application.copilot.host_store import CopilotHostStore


def prepare_contract_with_existing_memory(
    contract: ExecutionContract,
    *,
    store: CopilotHostStore,
    session_key: str,
) -> ExecutionContract:
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


def _memory_context(memory: dict[str, Any]) -> str:
    pinned = memory.get("pinned_state") if isinstance(memory.get("pinned_state"), dict) else {}
    episodes = [dict(item) for item in memory.get("episodes") or () if isinstance(item, dict)]
    if not pinned and not episodes:
        return ""
    return (
        "Conversation memory from earlier turns. This is context, not executable state. "
        "Tool findings recorded here are historical snapshots and may be stale — for any "
        "question about live candidates, runs, positions, notifications, or current status, "
        "call the relevant read-only tool again instead of repeating recorded tool conclusions. "
        "Current pending Control operations supplied separately remain authoritative.\n"
        + json.dumps(
            {"pinned_state": pinned, "recent_episodes": episodes[-3:]},
            ensure_ascii=False,
            default=str,
        )
    )


__all__ = ["prepare_contract_with_existing_memory"]
