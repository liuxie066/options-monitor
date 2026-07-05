from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any

from src.application.assistant.time_filters import extract_month_filters
from src.application.assistant.task_contract import TASK_CONTRACT_SCHEMA_VERSION, build_task_contract
from src.application.assistant.task_profiles import TaskProfile, select_task_profiles


AGENT_TASK_SCHEMA_VERSION = "om-agent-task-v1"


@dataclass(frozen=True)
class AgentTask:
    name: str
    goal: str
    domain: str
    task_mode: str
    requested_effect: str
    scope: dict[str, Any]
    profile_names: tuple[str, ...]
    required_evidence: tuple[str, ...]
    required_views: tuple[str, ...]
    required_answer: tuple[str, ...]
    answer_shape: tuple[str, ...]
    profiles: tuple[TaskProfile, ...] = ()
    schema_version: str = AGENT_TASK_SCHEMA_VERSION

    @property
    def requires_synthesis(self) -> bool:
        return bool(self.profile_names and self.task_mode in {"analyze", "compare", "diagnose", "recommend"})

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "goal": self.goal,
            "domain": self.domain,
            "task_mode": self.task_mode,
            "requested_effect": self.requested_effect,
            "scope": dict(self.scope),
            "profile_names": list(self.profile_names),
            "required_evidence": list(self.required_evidence),
            "required_views": list(self.required_views),
            "required_answer": list(self.required_answer),
            "answer_shape": list(self.answer_shape),
            "requires_synthesis": self.requires_synthesis,
        }

    def task_contract_patch(self) -> dict[str, Any]:
        return {
            "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
            "agent_task": self.public_payload(),
            "task_profiles": list(self.profile_names),
            "required_evidence": list(self.required_evidence),
            "required_views": list(self.required_views),
            "required_answer": list(self.required_answer),
            "answer_shape": list(self.answer_shape),
        }


def derive_agent_task(
    *,
    question: str,
    request_context: dict[str, Any] | None,
    today: date,
    conversation_context: dict[str, Any] | None,
) -> AgentTask:
    profile_text = _profile_text(question=question, conversation_context=conversation_context)
    base = build_task_contract(
        question=question,
        plan={"goal": question, "steps": []},
        request_context=request_context,
        today=today,
    )
    profiles = select_task_profiles(text=profile_text, domain=base.domain, task_mode=base.task_mode)
    primary = profiles[0] if profiles else None
    scope = dict(base.scope)
    months = extract_month_filters(profile_text, today=today)
    months.extend(_context_slot_values(conversation_context, "month"))
    if months:
        scope["requested_months"] = _unique(months)
        scope["planned_months"] = _unique([*scope.get("planned_months", []), *months])
    return AgentTask(
        name=primary.name if primary else base.domain,
        goal=base.goal,
        domain=_profile_domain(primary, base.domain),
        task_mode=base.task_mode,
        requested_effect=base.requested_effect,
        scope=scope,
        profile_names=tuple(profile.name for profile in profiles),
        required_evidence=_merge(base.required_evidence, *(profile.required_evidence for profile in profiles)),
        required_views=_merge(*(profile.required_views for profile in profiles)),
        required_answer=_merge(base.required_answer, *(profile.required_answer for profile in profiles)),
        answer_shape=_merge(base.answer_shape, *(profile.answer_shape for profile in profiles)),
        profiles=profiles,
    )


def _merge(*groups: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            text = str(item or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return tuple(out)


def _profile_domain(profile: TaskProfile | None, fallback: str) -> str:
    if profile is None:
        return fallback
    return profile.domains[0]


def _profile_text(*, question: str, conversation_context: dict[str, Any] | None) -> str:
    if not _is_short_contextual_followup(question):
        return str(question or "")
    projection = conversation_context.get("context_projection") if isinstance(conversation_context, dict) else {}
    if not isinstance(projection, dict):
        return str(question or "")
    turn = _latest_compatible_turn(question=question, turns=[item for item in projection.get("recent_turns") or [] if isinstance(item, dict)])
    if turn is None:
        return str(question or "")
    return "\n".join(
        item
        for item in (
            str(question or ""),
            str(turn.get("user_summary") or ""),
            str(turn.get("assistant_summary") or ""),
        )
        if item.strip()
    )


def _is_short_contextual_followup(question: str) -> bool:
    compact = re.sub(r"\s+", "", str(question or "").lower())
    if not compact or len(compact) > 12:
        return False
    return any(token in compact for token in ("结论", "总结", "继续", "这个", "上面", "刚才"))


def _latest_compatible_turn(*, question: str, turns: list[dict[str, Any]]) -> dict[str, Any] | None:
    for turn in reversed(turns):
        text = "\n".join(
            item
            for item in (
                str(question or ""),
                str(turn.get("user_summary") or ""),
                str(turn.get("assistant_summary") or ""),
            )
            if item.strip()
        )
        if select_task_profiles(text=text, domain="general", task_mode="analyze"):
            return turn
    return None


def _context_slot_values(conversation_context: dict[str, Any] | None, slot_key: str) -> list[str]:
    projection = conversation_context.get("context_projection") if isinstance(conversation_context, dict) else {}
    if not isinstance(projection, dict):
        return []
    values: list[str] = []
    for source_name in ("available_evidence_refs", "recent_turns"):
        for item in projection.get(source_name) or []:
            if not isinstance(item, dict):
                continue
            safe_slots = item.get("safe_slots") if isinstance(item.get("safe_slots"), dict) else {}
            values.extend(str(value) for value in safe_slots.get(slot_key) or [] if str(value).strip())
    return values


def _unique(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


__all__ = ["AGENT_TASK_SCHEMA_VERSION", "AgentTask", "derive_agent_task"]
