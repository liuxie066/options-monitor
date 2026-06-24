from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.memory import (
    ASSISTANT_MEMORY_TYPES,
    default_assistant_memory_dir,
)
from src.application.write_contract import attach_write_contract
from src.infrastructure.io_utils import atomic_write_json, atomic_write_text


MEMORY_PROPOSAL_SCHEMA_VERSION = "om-assistant-memory-proposal-v1"
DEFAULT_MEMORY_PROPOSALS_DIRNAME = "proposals"
MEMORY_PROPOSAL_STATUSES = frozenset({"proposed", "accepted", "rejected"})

_PROPOSAL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{3,96}$")
_SENSITIVE_TOKENS = (
    "access_token",
    "api key",
    "api_key",
    "authorization",
    "cookie",
    "password",
    "private_key",
    "secret",
    "token",
    "webhook",
)
_EXPLICIT_MEMORY_SIGNAL_RE = re.compile(
    r"记住|记一下|帮我记|以后|下次|我偏好|我的偏好|我的习惯|我习惯|我希望|纠正|更正|不是.+而是|"
    r"remember|prefer|preference|next time|correction|not .+ but",
    re.IGNORECASE,
)
_CURRENT_FACT_MARKERS = (
    "today",
    "yesterday",
    "now",
    "current",
    "latest",
    "今天",
    "昨天",
    "现在",
    "当前",
    "最新",
    "实时",
    "刚刚",
)
_RUNTIME_OR_MARKET_FACT_MARKERS = (
    "broker",
    "cash",
    "config",
    "holding",
    "open position",
    "order",
    "price",
    "runtime",
    "status",
    "through filter",
    "余额",
    "持仓",
    "成交",
    "订单",
    "现金",
    "价格",
    "筛选通过",
    "股价",
    "运行状态",
    "配置",
)
_PARAMETER_VALUE_MARKERS = (
    "delta",
    "iv",
    "min_",
    "max_",
    "strike",
    "threshold",
    "参数",
    "配置",
    "阈值",
    "行权价",
)
_SUGGESTION_TYPE_PROFILES = {
    "collaboration_preference": {
        "memory_id": "collaboration-preference",
        "title": "Collaboration preference",
        "tags": ["collaboration"],
    },
    "om_usage_preference": {
        "memory_id": "om-usage-preference",
        "title": "OM usage preference",
        "tags": ["om-usage"],
    },
    "parameter_tuning_preference": {
        "memory_id": "parameter-tuning-preference",
        "title": "Parameter tuning preference",
        "tags": ["parameter-tuning"],
    },
    "parameter_change_rationale": {
        "memory_id": "parameter-change-rationale",
        "title": "Parameter change rationale",
        "tags": ["parameter-tuning", "rationale"],
    },
    "correction_feedback": {
        "memory_id": "correction-feedback",
        "title": "Correction feedback",
        "tags": ["correction"],
    },
    "terminology": {
        "memory_id": "terminology",
        "title": "Terminology",
        "tags": ["terminology"],
    },
    "workflow_pattern": {
        "memory_id": "workflow-pattern",
        "title": "Workflow pattern",
        "tags": ["workflow"],
    },
}


def default_memory_proposals_dir(*, memory_dir: str | Path | None = None) -> Path:
    base = Path(memory_dir).expanduser().resolve() if memory_dir else default_assistant_memory_dir()
    return (base / DEFAULT_MEMORY_PROPOSALS_DIRNAME).resolve()


def save_memory_proposal(
    *,
    memory_type: str,
    title: str,
    summary: str,
    content: str,
    memory_dir: str | Path | None = None,
    memory_id: str | None = None,
    proposal_id: str | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
    source_turn: str | None = None,
    why_remember: str | None = None,
    risk_check: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    proposal = _build_proposal(
        memory_type=memory_type,
        title=title,
        summary=summary,
        content=content,
        memory_id=memory_id,
        proposal_id=proposal_id,
        tags=tags,
        source_turn=source_turn,
        why_remember=why_remember,
        risk_check=risk_check,
        created_at=created_at,
    )
    path = _proposal_path(memory_dir=memory_dir, proposal_id=proposal["proposal_id"])
    if path.exists():
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"assistant memory proposal already exists: {proposal['proposal_id']}",
        )
    atomic_write_json(path, proposal, sort_keys=True)
    return attach_write_contract(
        {
            "schema_version": MEMORY_PROPOSAL_SCHEMA_VERSION,
            "action": "propose",
            "proposal": _public_proposal(proposal),
            "proposal_path": str(path),
            "memory_dir": str(_memory_dir(memory_dir)),
        },
        dry_run=False,
        write_applied=True,
        rollback_hint=f"delete {path}",
    )


def list_memory_proposals(
    *,
    memory_dir: str | Path | None = None,
    status: str | None = "proposed",
    limit: int = 50,
) -> dict[str, Any]:
    normalized_status = _normalize_status_filter(status)
    proposals = []
    for proposal in _load_all_proposals(memory_dir=memory_dir):
        if normalized_status is not None and proposal.get("status") != normalized_status:
            continue
        proposals.append(_public_proposal(proposal))
    proposals = sorted(proposals, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    proposals = proposals[: max(0, min(int(limit or 50), 200))]
    return {
        "schema_version": MEMORY_PROPOSAL_SCHEMA_VERSION,
        "memory_dir": str(_memory_dir(memory_dir)),
        "proposal_dir": str(default_memory_proposals_dir(memory_dir=memory_dir)),
        "status": normalized_status or "all",
        "proposal_count": len(proposals),
        "proposals": proposals,
        "response_text": format_memory_proposals_text(proposals, status=normalized_status or "all"),
    }


def accept_memory_proposal(
    *,
    proposal_id: str,
    memory_dir: str | Path | None = None,
    memory_id: str | None = None,
    replace: bool = False,
    accepted_at: str | None = None,
) -> dict[str, Any]:
    proposal = _load_proposal_or_raise(memory_dir=memory_dir, proposal_id=proposal_id)
    if proposal.get("status") != "proposed":
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"assistant memory proposal is not proposed: {proposal_id}",
            details={"status": proposal.get("status")},
        )
    target_memory_id = _normalize_memory_id(memory_id or proposal.get("memory_id") or proposal.get("title"))
    target_path = (_memory_dir(memory_dir) / f"{target_memory_id}.md").resolve()
    if target_path.exists() and not replace:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"assistant memory already exists: {target_memory_id}",
            hint="Pass --replace only when intentionally replacing this memory file.",
            details={"memory_path": str(target_path)},
        )
    _validate_proposal_payload(proposal)
    markdown = _memory_markdown(proposal=proposal, memory_id=target_memory_id)
    atomic_write_text(target_path, markdown, encoding="utf-8")
    now = accepted_at or _utc_now()
    updated = dict(proposal)
    updated.update(
        {
            "status": "accepted",
            "accepted_at": now,
            "updated_at": now,
            "accepted_memory_id": target_memory_id,
            "accepted_memory_path": str(target_path),
        }
    )
    _write_proposal(memory_dir=memory_dir, proposal=updated)
    return attach_write_contract(
        {
            "schema_version": MEMORY_PROPOSAL_SCHEMA_VERSION,
            "action": "accept",
            "proposal": _public_proposal(updated),
            "memory_id": target_memory_id,
            "memory_path": str(target_path),
            "memory_dir": str(_memory_dir(memory_dir)),
        },
        dry_run=False,
        write_applied=True,
        rollback_hint=f"delete or edit {target_path}; proposal status is stored in {str(_proposal_path(memory_dir=memory_dir, proposal_id=proposal_id))}",
    )


def reject_memory_proposal(
    *,
    proposal_id: str,
    memory_dir: str | Path | None = None,
    reason: str | None = None,
    rejected_at: str | None = None,
) -> dict[str, Any]:
    proposal = _load_proposal_or_raise(memory_dir=memory_dir, proposal_id=proposal_id)
    if proposal.get("status") != "proposed":
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"assistant memory proposal is not proposed: {proposal_id}",
            details={"status": proposal.get("status")},
        )
    now = rejected_at or _utc_now()
    updated = dict(proposal)
    updated.update(
        {
            "status": "rejected",
            "rejected_at": now,
            "updated_at": now,
            "rejection_reason": _clip(reason, 500),
        }
    )
    _write_proposal(memory_dir=memory_dir, proposal=updated)
    return attach_write_contract(
        {
            "schema_version": MEMORY_PROPOSAL_SCHEMA_VERSION,
            "action": "reject",
            "proposal": _public_proposal(updated),
            "proposal_path": str(_proposal_path(memory_dir=memory_dir, proposal_id=proposal_id)),
            "memory_dir": str(_memory_dir(memory_dir)),
        },
        dry_run=False,
        write_applied=True,
        rollback_hint=f"edit {str(_proposal_path(memory_dir=memory_dir, proposal_id=proposal_id))} if this rejection was accidental",
    )


def suggest_memory_proposals_from_text(
    *,
    text: str,
    memory_dir: str | Path | None = None,
    source_turn: str | None = None,
    max_suggestions: int = 3,
    write: bool = True,
) -> dict[str, Any]:
    source_text = str(text or "").strip()
    if not source_text:
        raise AgentToolError(code="INPUT_ERROR", message="assistant memory suggest text is required")

    suggestions, skipped = _build_suggestions_from_text(
        text=source_text,
        source_turn=source_turn,
        max_suggestions=max_suggestions,
    )
    proposals: list[dict[str, Any]] = []
    proposal_paths: list[str] = []
    if write:
        for suggestion in suggestions:
            saved = save_memory_proposal(memory_dir=memory_dir, **suggestion)
            proposal = saved.get("proposal") if isinstance(saved.get("proposal"), dict) else {}
            if proposal:
                proposals.append(proposal)
            path = str(saved.get("proposal_path") or "").strip()
            if path:
                proposal_paths.append(path)
    else:
        for suggestion in suggestions:
            proposal = _build_proposal(**suggestion)
            proposals.append(_public_proposal(proposal))

    payload = {
        "schema_version": MEMORY_PROPOSAL_SCHEMA_VERSION,
        "action": "suggest",
        "source": "explicit_text",
        "memory_dir": str(_memory_dir(memory_dir)),
        "suggestion_count": len(suggestions),
        "proposal_count": len(proposals),
        "proposals": proposals,
        "proposal_paths": proposal_paths,
        "skipped": skipped,
    }
    payload["response_text"] = format_memory_suggestions_text(payload)
    rollback_hint = "delete generated proposal JSON files" if proposal_paths else None
    return attach_write_contract(
        payload,
        dry_run=not bool(write),
        write_applied=bool(write and proposals),
        rollback_hint=rollback_hint,
    )


def format_memory_proposals_text(proposals: list[dict[str, Any]], *, status: str = "proposed") -> str:
    if not proposals:
        return f"No assistant memory proposals found for status={status}."
    lines = [f"Assistant memory proposals status={status} count={len(proposals)}"]
    for item in proposals:
        tags = ", ".join(item.get("tags") or [])
        suffix = f" tags=[{tags}]" if tags else ""
        lines.append(
            f"- {item.get('proposal_id')} | {item.get('status')} | {item.get('type')} | "
            f"{item.get('title')}{suffix}"
        )
    return "\n".join(lines)


def format_memory_suggestions_text(payload: dict[str, Any]) -> str:
    proposals = payload.get("proposals") if isinstance(payload.get("proposals"), list) else []
    if proposals:
        return format_memory_proposals_text(proposals, status="suggested")
    skipped = payload.get("skipped") if isinstance(payload.get("skipped"), list) else []
    reasons = sorted({str(item.get("reason") or "") for item in skipped if isinstance(item, dict) and item.get("reason")})
    suffix = f" reason={','.join(reasons)}" if reasons else ""
    return f"No safe assistant memory suggestions found.{suffix}"


def _build_proposal(
    *,
    memory_type: str,
    title: str,
    summary: str,
    content: str,
    memory_id: str | None,
    proposal_id: str | None,
    tags: list[str] | tuple[str, ...] | None,
    source_turn: str | None,
    why_remember: str | None,
    risk_check: str | None,
    created_at: str | None,
) -> dict[str, Any]:
    memory_type = str(memory_type or "").strip()
    if memory_type not in ASSISTANT_MEMORY_TYPES:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unsupported assistant memory type: {memory_type}",
            details={"supported_types": sorted(ASSISTANT_MEMORY_TYPES)},
        )
    title_text = _required_text(title, "title", limit=160)
    summary_text = _required_text(summary, "summary", limit=320)
    content_text = _required_text(content, "content", limit=2400)
    tag_items = _normalize_tags(tags)
    memory_id_text = _normalize_memory_id(memory_id or title_text)
    proposal_id_text = _normalize_proposal_id(proposal_id or _make_proposal_id(title_text))
    now = created_at or _utc_now()
    proposal = {
        "schema_version": MEMORY_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id_text,
        "memory_id": memory_id_text,
        "type": memory_type,
        "title": title_text,
        "summary": summary_text,
        "content": content_text,
        "tags": tag_items,
        "source_turn": _clip(source_turn, 240),
        "why_remember": _clip(why_remember, 600),
        "risk_check": _clip(risk_check, 600),
        "status": "proposed",
        "created_at": now,
        "updated_at": now,
        "policy": {
            "proposal_requires_explicit_accept": True,
            "memory_is_hint_only": True,
            "memory_cannot_authorize_writes": True,
            "tool_evidence_wins_memory": True,
        },
    }
    _validate_proposal_payload(proposal)
    return proposal


def _build_suggestions_from_text(
    *,
    text: str,
    source_turn: str | None,
    max_suggestions: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    skipped: list[dict[str, Any]] = []
    reason = _memory_suggestion_skip_reason(text)
    if reason:
        return [], [{"reason": reason}]

    limit = max(0, min(int(max_suggestions or 0), 5))
    if limit <= 0:
        return [], [{"reason": "limit_zero"}]

    memory_type = _infer_suggested_memory_type(text)
    profile = _SUGGESTION_TYPE_PROFILES[memory_type]
    candidate_text = _clean_suggested_memory_text(text)
    summary = _suggestion_summary(memory_type=memory_type, text=candidate_text)
    content = _suggestion_content(memory_type=memory_type, text=candidate_text)
    return [
        {
            "memory_type": memory_type,
            "memory_id": str(profile["memory_id"]),
            "title": str(profile["title"]),
            "summary": summary,
            "content": content,
            "proposal_id": None,
            "tags": list(profile.get("tags") or []),
            "source_turn": source_turn,
            "why_remember": "The text contains an explicit remember, preference, or correction signal.",
            "risk_check": (
                "Proposal only; accepted memory remains hint-only and cannot authorize writes. "
                "Skipped current market/runtime facts, config values, and obvious secrets."
            ),
            "created_at": None,
        }
    ][:limit], skipped


def _memory_suggestion_skip_reason(text: str) -> str | None:
    source = str(text or "").strip()
    if not _EXPLICIT_MEMORY_SIGNAL_RE.search(source):
        return "missing_explicit_memory_signal"
    if len(source) < 8:
        return "too_short"
    if _sensitive_lines(source):
        return "sensitive_material"
    if _looks_like_runtime_or_market_fact(source):
        return "runtime_or_market_fact"
    if _looks_like_parameter_value_fact(source):
        return "config_or_parameter_value"
    return None


def _infer_suggested_memory_type(text: str) -> str:
    lowered = str(text or "").lower()
    if re.search(r"纠正|更正|不是.+而是|correction|not .+ but", lowered, re.IGNORECASE):
        return "correction_feedback"
    if re.search(r"术语|叫做|应该叫|意思是|term|terminology|means", lowered, re.IGNORECASE):
        return "terminology"
    if re.search(r"参数|调参|阈值|候选|过滤|通过率|replay|delta|liquidity|threshold", lowered, re.IGNORECASE):
        if re.search(r"因为|变更原因|改动原因|调整原因|rationale|because", lowered, re.IGNORECASE):
            return "parameter_change_rationale"
        return "parameter_tuning_preference"
    if re.search(r"流程|工作流|步骤|先.+再|workflow|process", lowered, re.IGNORECASE):
        return "workflow_pattern"
    if re.search(r"配合|共创|语气|风格|偏好|习惯|collaboration|tone|style|prefer", lowered, re.IGNORECASE):
        return "collaboration_preference"
    return "om_usage_preference"


def _looks_like_runtime_or_market_fact(text: str) -> bool:
    lowered = str(text or "").lower()
    has_current_marker = any(marker in lowered for marker in _CURRENT_FACT_MARKERS)
    has_fact_marker = any(marker in lowered for marker in _RUNTIME_OR_MARKET_FACT_MARKERS)
    return bool(has_current_marker and has_fact_marker)


def _looks_like_parameter_value_fact(text: str) -> bool:
    lowered = str(text or "").lower()
    if not re.search(r"\d+(?:\.\d+)?", lowered):
        return False
    return any(marker in lowered for marker in _PARAMETER_VALUE_MARKERS)


def _clean_suggested_memory_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    cleaned = re.sub(r"^(请)?(帮我)?(记住|记一下)[:：,，\s]*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(remember|please remember)[:：,，\s]*", "", cleaned, flags=re.IGNORECASE)
    return _clip(cleaned, 600)


def _suggestion_summary(*, memory_type: str, text: str) -> str:
    prefix = {
        "collaboration_preference": "Explicit collaboration preference:",
        "om_usage_preference": "Explicit OM usage preference:",
        "parameter_tuning_preference": "Explicit parameter-tuning preference:",
        "parameter_change_rationale": "Explicit parameter-change rationale:",
        "correction_feedback": "Explicit correction feedback:",
        "terminology": "Explicit terminology preference:",
        "workflow_pattern": "Explicit workflow preference:",
    }.get(memory_type, "Explicit assistant memory preference:")
    return _clip(f"{prefix} {text}", 320)


def _suggestion_content(*, memory_type: str, text: str) -> str:
    if memory_type == "correction_feedback":
        return _clip(f"Use this correction in later OM assistant turns: {text}", 1200)
    if memory_type == "terminology":
        return _clip(f"Use this terminology preference in later OM assistant turns: {text}", 1200)
    if memory_type == "parameter_change_rationale":
        return _clip(f"Use this rationale as hint-only context when discussing OM parameter changes: {text}", 1200)
    if memory_type == "parameter_tuning_preference":
        return _clip(f"Use this as hint-only context when collaborating on OM parameter tuning: {text}", 1200)
    if memory_type == "workflow_pattern":
        return _clip(f"Use this workflow preference as hint-only context for later OM assistant work: {text}", 1200)
    if memory_type == "collaboration_preference":
        return _clip(f"Use this collaboration preference as hint-only context in later OM assistant turns: {text}", 1200)
    return _clip(f"Use this OM usage preference as hint-only context in later assistant turns: {text}", 1200)


def _validate_proposal_payload(proposal: dict[str, Any]) -> None:
    for key in ("title", "summary", "content", "why_remember", "risk_check", "tags"):
        if key == "tags":
            values = proposal.get("tags") if isinstance(proposal.get("tags"), list) else []
            text = "\n".join(str(item or "") for item in values)
        else:
            text = str(proposal.get(key) or "")
        sensitive = _sensitive_lines(text)
        if sensitive:
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"assistant memory proposal contains sensitive material in {key}",
                details={"field": key, "sensitive_line_count": len(sensitive)},
            )


def _memory_markdown(*, proposal: dict[str, Any], memory_id: str) -> str:
    tags = proposal.get("tags") if isinstance(proposal.get("tags"), list) else []
    tags_text = "[" + ", ".join(_quote_yaml_string(str(item)) for item in tags) + "]"
    lines = [
        "---",
        "type: " + str(proposal.get("type") or ""),
        "title: " + _quote_yaml_string(str(proposal.get("title") or "")),
        "summary: " + _quote_yaml_string(str(proposal.get("summary") or "")),
        f"tags: {tags_text}",
        "status: accepted",
        "source: memory_proposal",
        "proposal_id: " + str(proposal.get("proposal_id") or ""),
        "memory_id: " + memory_id,
        "---",
        "",
        str(proposal.get("content") or "").strip(),
        "",
    ]
    return "\n".join(lines)


def _load_all_proposals(*, memory_dir: str | Path | None) -> list[dict[str, Any]]:
    proposal_dir = default_memory_proposals_dir(memory_dir=memory_dir)
    if not proposal_dir.exists() or not proposal_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(proposal_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and data.get("schema_version") == MEMORY_PROPOSAL_SCHEMA_VERSION:
            out.append(data)
    return out


def _load_proposal_or_raise(*, memory_dir: str | Path | None, proposal_id: str) -> dict[str, Any]:
    normalized = _normalize_proposal_id(proposal_id)
    path = _proposal_path(memory_dir=memory_dir, proposal_id=normalized)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"assistant memory proposal not found: {normalized}") from exc
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"failed to read assistant memory proposal: {normalized}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != MEMORY_PROPOSAL_SCHEMA_VERSION:
        raise AgentToolError(code="INPUT_ERROR", message=f"invalid assistant memory proposal: {normalized}")
    return data


def _write_proposal(*, memory_dir: str | Path | None, proposal: dict[str, Any]) -> None:
    proposal_id = _normalize_proposal_id(proposal.get("proposal_id"))
    atomic_write_json(_proposal_path(memory_dir=memory_dir, proposal_id=proposal_id), proposal, sort_keys=True)


def _proposal_path(*, memory_dir: str | Path | None, proposal_id: str) -> Path:
    normalized = _normalize_proposal_id(proposal_id)
    return (default_memory_proposals_dir(memory_dir=memory_dir) / f"{normalized}.json").resolve()


def _memory_dir(memory_dir: str | Path | None) -> Path:
    if memory_dir:
        return Path(memory_dir).expanduser().resolve()
    return default_assistant_memory_dir()


def _normalize_status_filter(status: str | None) -> str | None:
    text = str(status or "proposed").strip().lower()
    if text in {"", "all"}:
        return None
    if text not in MEMORY_PROPOSAL_STATUSES:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unsupported assistant memory proposal status: {status}",
            details={"supported_statuses": sorted(MEMORY_PROPOSAL_STATUSES) + ["all"]},
        )
    return text


def _normalize_proposal_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or not _PROPOSAL_ID_RE.fullmatch(text):
        raise AgentToolError(
            code="INPUT_ERROR",
            message="assistant memory proposal id must be 3-96 characters using letters, digits, '_', '-', or '.'",
        )
    return text


def _normalize_memory_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    if not text:
        text = "memory-" + uuid4().hex[:8]
    return text[:80]


def _make_proposal_id(title: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"memprop_{stamp}_{_normalize_memory_id(title)[:32]}_{uuid4().hex[:8]}"


def _required_text(value: Any, field: str, *, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgentToolError(code="INPUT_ERROR", message=f"assistant memory proposal {field} is required")
    return _clip(text, limit)


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[: max(0, int(limit))]


def _normalize_tags(tags: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in tags or ():
        text = _clip(item, 80)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= 8:
            break
    return out


def _public_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "proposal_id",
        "memory_id",
        "type",
        "title",
        "summary",
        "content",
        "tags",
        "source_turn",
        "why_remember",
        "risk_check",
        "status",
        "created_at",
        "updated_at",
        "accepted_at",
        "rejected_at",
        "accepted_memory_id",
        "accepted_memory_path",
        "rejection_reason",
        "policy",
    )
    return {key: proposal.get(key) for key in keys if proposal.get(key) not in (None, "", [], {})}


def _sensitive_lines(content: str) -> list[str]:
    out: list[str] = []
    for line in str(content or "").splitlines():
        lowered = line.lower()
        if any(token in lowered for token in _SENSITIVE_TOKENS) and any(
            marker in lowered for marker in ("=", ":", "sk-", "bearer ", "http://", "https://")
        ):
            out.append(line)
    return out


def _quote_yaml_string(value: str) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "DEFAULT_MEMORY_PROPOSALS_DIRNAME",
    "MEMORY_PROPOSAL_SCHEMA_VERSION",
    "MEMORY_PROPOSAL_STATUSES",
    "accept_memory_proposal",
    "default_memory_proposals_dir",
    "format_memory_proposals_text",
    "format_memory_suggestions_text",
    "list_memory_proposals",
    "reject_memory_proposal",
    "save_memory_proposal",
    "suggest_memory_proposals_from_text",
]
