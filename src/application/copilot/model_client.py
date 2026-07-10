from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from src.application.llm_provider_registry import (
    provider_api_kind,
    provider_chat_completion_payload_options,
    require_provider_spec,
)
from src.infrastructure.openai_chat_completions import (
    create_json_chat_completion,
    extract_chat_completion_text,
)
from src.infrastructure.openai_responses import (
    create_structured_response,
    extract_response_text,
)


ModelCallable = Callable[[dict[str, Any]], dict[str, Any]]
CreateResponseFn = Callable[..., dict[str, Any]]


ACTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "tool_name", "reason", "answer_report"],
    "properties": {
        "kind": {"type": "string", "enum": ["tool", "finish"]},
        "tool_name": {"type": ["string", "null"]},
        "reason": {"type": ["string", "null"]},
        "answer_report": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": [
                "conclusion",
                "attempted_checks",
                "findings",
                "recommendations",
                "missing_data",
                "evidence_refs",
            ],
            "properties": {
                "conclusion": {"type": "string"},
                "attempted_checks": {"type": "array", "items": {"type": "string"}},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["summary", "evidence_refs"],
                        "properties": {
                            "summary": {"type": "string"},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "recommendations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["summary", "action", "target_scope", "answer_dimension", "basis_refs"],
                        "properties": {
                            "summary": {
                                "type": "string",
                                "description": "Concrete next-step summary; do not use a generic follow-up.",
                            },
                            "action": {
                                "type": "string",
                                "description": "The concrete read-only next action or operator decision to take.",
                            },
                            "target_scope": {
                                "type": "string",
                                "description": "The entity, account, metric, date range, or review scope this recommendation applies to.",
                            },
                            "answer_dimension": {
                                "type": ["string", "null"],
                                "description": (
                                    "Use one task_guidance.answer_dimensions value when present; otherwise null."
                                ),
                            },
                            "basis_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Observation refs that support this recommendation.",
                            },
                        },
                    },
                },
                "missing_data": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

ACTION_INSTRUCTIONS = """You are the action selector inside OM Copilot.
Return one JSON object only.
Choose "tool" only from allowed_tools when another read-only observation is needed.
When kind is "tool", set answer_report to null.
Choose "finish" only when the observations are sufficient.
When kind is "finish", set tool_name to null and answer_report to an object.
Use task_guidance only for scene evidence expectations and stopping conditions.
Use quality_contract as the output contract when deciding whether a finish report is good enough.
Use each observation's evidence_context to distinguish requested-scope evidence, current context, latest context, and evidence boundaries.
Do not finish with raw rows, receipt-like field dumps, or a generic "analysis completed" answer.
Respect finish_conditions. If unattempted_tools_without_evidence is non-empty, choose a tool from that list before finishing.
Do not choose a tool already listed in attempted_tools; failed or weak attempted tools are missing evidence, not retry targets.
Use attempted_tools_without_evidence only to understand which already-attempted tools still lack usable evidence.
If finish_conditions.missing_allowed_tool_evidence lists a failed or weak attempted tool, copy the relevant missing_data text into answer_report.missing_data when finishing.
If finish_conditions.requires_cited_findings is true, a finish answer_report must include at least one finding with observation refs.
If finish_conditions.requires_recommendations is true and finish_conditions.unattempted_tools_without_evidence plus finish_conditions.missing_allowed_tool_evidence are both empty, a finish answer_report must include at least one recommendation with basis_refs from finish_conditions.claimable_refs.
If finish_conditions.requires_recommendations is true but finish_conditions.missing_allowed_tool_evidence is non-empty, do not invent recommendations; finish with missing_data instead.
Every recommendation basis_refs list should overlap at least one cited finding's evidence_refs.
When finishing, answer_report.conclusion must start with "结论" and be supported by cited findings.
If the conclusion is explicitly about missing evidence, the gap must appear in answer_report.missing_data.
Do not use refs beyond their evidence_context boundary.
Every finding must cite ref values listed in finish_conditions.claimable_refs.
Use finish_conditions.claimable_refs_by_tool to choose refs from the tool observations that directly support each finding or recommendation.
Use finish_conditions.claimable_ref_context to check each cited ref's tool, time scope, record type, and non-use boundary.
Use finish_conditions.requested_scope_refs for scoped evidence and finish_conditions.current_context_refs only for current/latest context.
If an observation has facts_omitted, do not make only/no other/all-style exhaustive claims from that ref.
When recommendations are useful, include answer_report.recommendations with action, target_scope, summary, basis_refs, and answer_dimension; set answer_dimension to one task_guidance.answer_dimensions value when present, otherwise null.
If execution_environment is "eval", the conclusion must say it is eval-only and answer_report.missing_data must include "fixture observations are not production evidence".
Do not claim writes, notifications, broker actions, config changes, deployments, or service changes."""


@dataclass(frozen=True)
class CopilotModelSettings:
    provider: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    timeout_seconds: int = 20
    max_output_tokens: int = 1024

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "CopilotModelSettings":
        cfg = raw if isinstance(raw, dict) else {}
        provider = str(cfg.get("provider") or "").strip().lower()
        spec = require_provider_spec(provider, path="copilot.model.provider")
        return cls(
            provider=spec.provider_id,
            model=str(cfg.get("model") or "").strip(),
            base_url=str(cfg.get("base_url") or spec.default_base_url).strip(),
            api_key_env=str(cfg.get("api_key_env") or spec.default_api_key_env).strip(),
            timeout_seconds=_bounded_int(cfg.get("timeout_seconds"), default=20, minimum=1, maximum=120),
            max_output_tokens=_bounded_int(cfg.get("max_output_tokens"), default=1024, minimum=128, maximum=4096),
        )


def build_action_model(
    settings: CopilotModelSettings,
    *,
    environ: dict[str, str] | None = None,
    create_response_fn: CreateResponseFn | None = None,
    create_chat_completion_fn: CreateResponseFn | None = None,
) -> ModelCallable:
    if not settings.model.strip():
        raise ValueError("model is required")
    api_key = _api_key_value(settings, environ=environ)
    if not api_key:
        raise ValueError("api key env is not configured")

    def _model(request: dict[str, Any]) -> dict[str, Any]:
        input_text = json.dumps(dict(request), ensure_ascii=False, sort_keys=True)
        if provider_api_kind(settings.provider) == "chat_completions":
            raw = _call_chat_completion(settings, api_key, input_text, create_chat_completion_fn)
            text = extract_chat_completion_text(raw)
        else:
            raw = (create_response_fn or create_structured_response)(
                api_key=api_key,
                base_url=settings.base_url,
                model=settings.model,
                input_text=input_text,
                instructions=ACTION_INSTRUCTIONS,
                json_schema=ACTION_JSON_SCHEMA,
                timeout=settings.timeout_seconds,
                max_output_tokens=settings.max_output_tokens,
                temperature=0.0,
            )
            text = extract_response_text(raw)
        return _parse_json_object(text)

    return _model


def _call_chat_completion(
    settings: CopilotModelSettings,
    api_key: str,
    input_text: str,
    create_chat_completion_fn: CreateResponseFn | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": settings.base_url,
        "model": settings.model,
        "input_text": input_text,
        "instructions": ACTION_INSTRUCTIONS,
        "json_schema": ACTION_JSON_SCHEMA,
        "timeout": settings.timeout_seconds,
        "max_output_tokens": settings.max_output_tokens,
    }
    kwargs.update(provider_chat_completion_payload_options(settings.provider))
    return (create_chat_completion_fn or create_json_chat_completion)(**kwargs)


def _api_key_value(settings: CopilotModelSettings, *, environ: dict[str, str] | None) -> str:
    env = environ if environ is not None else os.environ
    return str(env.get(settings.api_key_env) or "").strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    value = _strip_json_code_fence(text)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("model action response must be an object")
    return parsed


def _strip_json_code_fence(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return max(int(minimum), min(parsed, int(maximum)))
