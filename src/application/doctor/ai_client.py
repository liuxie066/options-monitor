from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.doctor.prompt import build_doctor_prompt


AiCompleteFn = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def resolve_ai_config(payload: dict[str, Any], runtime_cfg: dict[str, Any]) -> dict[str, Any]:
    cfg_raw = runtime_cfg.get("doctor") if isinstance(runtime_cfg.get("doctor"), dict) else {}
    doctor_cfg: dict[str, Any] = cfg_raw if isinstance(cfg_raw, dict) else {}
    ai_raw = doctor_cfg.get("ai") if isinstance(doctor_cfg.get("ai"), dict) else {}
    resolved: dict[str, Any] = dict(ai_raw if isinstance(ai_raw, dict) else {})
    payload_ai = payload.get("ai_config")
    if isinstance(payload_ai, dict):
        resolved.update(payload_ai)

    resolved["enabled"] = bool(payload.get("ai", resolved.get("enabled", False)))
    resolved["provider"] = str(resolved.get("provider") or "openai_compatible")
    resolved["timeout_seconds"] = _as_int(resolved.get("timeout_seconds"), default=60, low=5, high=300)
    resolved["max_input_chars"] = _as_int(resolved.get("max_input_chars"), default=60000, low=1000, high=300000)
    return resolved


def maybe_run_ai_triage(
    *,
    payload: dict[str, Any],
    runtime_cfg: dict[str, Any],
    redacted_evidence: dict[str, Any],
    diagnosis: dict[str, Any],
    ai_complete_fn: AiCompleteFn | None = None,
) -> tuple[dict[str, Any] | None, list[str], dict[str, Any]]:
    ai_config = resolve_ai_config(payload, runtime_cfg)
    meta = _safe_ai_meta(ai_config)
    if not ai_config.get("enabled"):
        return None, [], meta

    missing = _missing_ai_config(ai_config)
    if missing:
        return (
            {
                "status": "unavailable",
                "category": "ai_unavailable",
                "confidence": "low",
                "problem": "AI triage was requested but AI configuration is incomplete.",
                "impact": "Deterministic doctor findings are still available.",
                "evidence": [],
                "strategy_observations": [],
                "strategy_improvement_directions": [],
                "suspected_code_area": [],
                "local_debug_steps": [],
                "issue_candidate": {"create_issue": False, "reason": f"missing AI config: {', '.join(missing)}"},
            },
            [f"ai_unavailable: missing AI config: {', '.join(missing)}"],
            meta,
        )

    try:
        if ai_complete_fn is not None:
            result = ai_complete_fn(
                {
                    "evidence": _compact_for_ai(redacted_evidence, max_chars=int(ai_config["max_input_chars"])),
                    "diagnosis": diagnosis,
                },
                ai_config,
            )
        else:
            result = call_openai_compatible(
                evidence=_compact_for_ai(redacted_evidence, max_chars=int(ai_config["max_input_chars"])),
                diagnosis=diagnosis,
                ai_config=ai_config,
            )
    except AgentToolError as exc:
        return (
            {
                "status": "unavailable",
                "category": "ai_unavailable",
                "confidence": "low",
                "problem": exc.message,
                "impact": "Deterministic doctor findings are still available.",
                "evidence": [],
                "strategy_observations": [],
                "strategy_improvement_directions": [],
                "suspected_code_area": [],
                "local_debug_steps": [],
                "issue_candidate": {"create_issue": False, "reason": "AI call failed"},
            },
            [f"ai_unavailable: {exc.message}"],
            meta,
        )
    normalized = normalize_ai_result(result)
    return normalized, [], meta


def call_openai_compatible(*, evidence: dict[str, Any], diagnosis: dict[str, Any], ai_config: dict[str, Any]) -> dict[str, Any]:
    api_key = _api_key(ai_config)
    url = _chat_completions_url(str(ai_config.get("base_url") or ""))
    body = {
        "model": str(ai_config.get("model") or ""),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an operations-quality doctor for an options monitoring system. "
                    "Return one strict JSON object only. Do not include markdown."
                ),
            },
            {"role": "user", "content": build_doctor_prompt(evidence=evidence, diagnosis=diagnosis)},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=int(ai_config.get("timeout_seconds") or 60)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AgentToolError(code="AI_ERROR", message=f"AI endpoint returned HTTP {exc.code}") from exc
    except Exception as exc:
        raise AgentToolError(code="AI_ERROR", message=f"{type(exc).__name__}: {exc}") from exc

    content = _extract_chat_content(payload)
    try:
        parsed = json.loads(content)
    except Exception as exc:
        raise AgentToolError(code="AI_ERROR", message="AI response was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise AgentToolError(code="AI_ERROR", message="AI response JSON must be an object")
    return parsed


def normalize_ai_result(value: dict[str, Any]) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    issue_raw = payload.get("issue_candidate")
    issue: dict[str, Any] = issue_raw if isinstance(issue_raw, dict) else {}
    return {
        "status": _choice(payload.get("status"), {"ok", "warn", "fail", "unavailable"}, default="warn"),
        "category": str(payload.get("category") or "insufficient_evidence"),
        "confidence": _choice(payload.get("confidence"), {"high", "medium", "low"}, default="low"),
        "problem": str(payload.get("problem") or "").strip(),
        "impact": str(payload.get("impact") or "").strip(),
        "evidence": _list_of_dicts(payload.get("evidence")),
        "ai_diagnosis": str(payload.get("ai_diagnosis") or payload.get("diagnosis") or "").strip(),
        "strategy_observations": _list_of_strings(payload.get("strategy_observations")),
        "strategy_improvement_directions": _list_of_strings(payload.get("strategy_improvement_directions")),
        "suspected_code_area": [str(item) for item in payload.get("suspected_code_area") or [] if str(item).strip()],
        "local_debug_steps": [str(item) for item in payload.get("local_debug_steps") or [] if str(item).strip()],
        "issue_candidate": {
            "create_issue": bool(issue.get("create_issue", False)),
            "reason": str(issue.get("reason") or "").strip(),
        },
    }


def _missing_ai_config(ai_config: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not str(ai_config.get("base_url") or "").strip():
        missing.append("base_url")
    if not str(ai_config.get("model") or "").strip():
        missing.append("model")
    if not _api_key(ai_config):
        missing.append(str(ai_config.get("api_key_env") or "api_key"))
    return missing


def _api_key(ai_config: dict[str, Any]) -> str:
    env_name = str(ai_config.get("api_key_env") or "").strip()
    if env_name:
        return str(os.environ.get(env_name) or "").strip()
    return str(ai_config.get("api_key") or "").strip()


def _chat_completions_url(base_url: str) -> str:
    raw = base_url.strip().rstrip("/")
    if raw.endswith("/chat/completions"):
        return raw
    return f"{raw}/chat/completions"


def _extract_chat_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            if isinstance(first.get("text"), str):
                return first["text"]
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        return part["text"]
    raise AgentToolError(code="AI_ERROR", message="AI response did not contain message content")


def _compact_for_ai(payload: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) <= max_chars:
        return payload
    return {
        "schema_version": payload.get("schema_version"),
        "truncated": True,
        "max_input_chars": max_chars,
        "head_json": text[:max_chars],
    }


def _safe_ai_meta(ai_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(ai_config.get("enabled")),
        "provider": ai_config.get("provider"),
        "base_url": ai_config.get("base_url"),
        "model": ai_config.get("model"),
        "api_key_env": ai_config.get("api_key_env"),
        "timeout_seconds": ai_config.get("timeout_seconds"),
        "max_input_chars": ai_config.get("max_input_chars"),
    }


def _as_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(low, min(high, parsed))


def _choice(value: Any, allowed: set[str], *, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
