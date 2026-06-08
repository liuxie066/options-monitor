from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.application.assistant.commands import llm_capability_manifest
from src.application.assistant.config_loader import load_assistant_config
from src.application.assistant.llm_common import (
    CreateStructuredResponseFn,
    is_supported_llm_provider,
    normalize_llm_provider,
    provider_api_kind,
    provider_endpoint_url,
    supported_llm_providers,
)
from src.application.assistant.agent_loop import plan_read_only_tools
from src.application.assistant.settings import AssistantSettings, LlmTranslatorSettings
from src.application.settings import build_effective_env
from src.infrastructure.openai_chat_completions import resolve_chat_completions_url
from src.infrastructure.openai_responses import resolve_responses_url


DEFAULT_LIVE_PROBE_TEXT = "状态"


def check_llm_translator(
    *,
    repo_root: str | Path,
    config_path: str | Path | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
    live: bool = False,
    live_text: str = DEFAULT_LIVE_PROBE_TEXT,
    create_response_fn: CreateStructuredResponseFn | None = None,
) -> dict[str, Any]:
    explicit_config_path = bool(config_path is not None and str(config_path).strip())
    path, cfg = load_assistant_config(
        config_path=config_path,
        repo_root=repo_root,
        missing_ok=not explicit_config_path,
    )
    runtime_settings = AssistantSettings.from_runtime_config(cfg)
    settings = runtime_settings.llm
    effective_env = build_effective_env(
        repo_root=repo_root,
        env_file=env_file,
        include_local_env_file=include_local_env_file,
    )

    checks: list[dict[str, Any]] = []
    validation_ok = _append_assistant_config_check(checks, cfg=cfg, settings=runtime_settings)
    checks.extend(_config_checks(settings, effective_env=effective_env))
    manifest = llm_capability_manifest()

    live_probe = _live_probe_check(
        runtime_settings=runtime_settings,
        effective_env=effective_env.values,
        live=bool(live),
        live_text=live_text,
        create_response_fn=create_response_fn,
    )
    checks.append(live_probe)

    blocking_statuses = {"error"}
    ok = bool(validation_ok) and not any(str(item.get("status")) in blocking_statuses for item in checks)
    if not settings.enabled and validation_ok:
        status = "disabled"
    elif ok:
        status = "ready"
    else:
        status = "error"

    return {
        "summary": {
            "ok": ok,
            "status": status,
            "enabled": bool(settings.enabled),
            "live_checked": bool(live),
            "error_count": sum(1 for item in checks if item.get("status") == "error"),
            "warning_count": sum(1 for item in checks if item.get("status") == "warn"),
        },
        "config": {
            "config_kind": "assistant",
            "config_path": str(path),
            "loaded": bool(cfg),
            "mode": runtime_settings.mode,
        },
        "env": {
            "env_file": str(effective_env.env_file) if effective_env.env_file is not None else None,
            "env_file_loaded": bool(effective_env.env_file_loaded),
            "warnings": list(effective_env.warnings),
        },
        "llm": {
            **settings.public_payload(),
            "endpoint_url": _provider_endpoint_url(settings),
            "responses_url": resolve_responses_url(settings.base_url) if _provider_api_kind(settings) == "responses" else None,
            "chat_completions_url": resolve_chat_completions_url(settings.base_url)
            if _provider_api_kind(settings) == "chat_completions"
            else None,
            "api_key_configured": bool(effective_env.get(settings.api_key_env)),
            "api_key_source": _source_value(effective_env.source_of(settings.api_key_env)),
        },
        "capabilities": _capability_summary(manifest),
        "checks": checks,
    }


def _append_assistant_config_check(checks: list[dict[str, Any]], *, cfg: dict[str, Any], settings: AssistantSettings) -> bool:
    if not cfg:
        checks.append({
            "name": "assistant_config",
            "status": "warn",
            "message": "config.assistant.json not found; using default AgentLoop settings",
        })
        return True
    if settings.mode not in {"agent_loop", "disabled"}:
        checks.append({
            "name": "assistant_config",
            "status": "error",
            "message": "assistant mode compatibility field is invalid",
        })
        return False
    checks.append({
        "name": "assistant_config",
        "status": "ok",
        "message": "assistant config validates",
    })
    return True


def _config_checks(settings: LlmTranslatorSettings, *, effective_env: Any) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append({
        "name": "enabled",
        "status": "ok" if settings.enabled else "warn",
        "message": "assistant planner uses LLM" if settings.enabled else "assistant planner is disabled; LLM is inactive",
    })
    checks.append(_provider_check(settings.provider, required=settings.enabled))
    checks.append(_required_text_check("model", settings.model, required=settings.enabled))
    checks.append(_required_text_check("api_key_env", settings.api_key_env, required=settings.enabled))

    api_key_source = effective_env.source_of(settings.api_key_env)
    api_key_configured = bool(effective_env.get(settings.api_key_env))
    checks.append({
        "name": "api_key",
        "status": "ok" if api_key_configured else ("error" if settings.enabled else "warn"),
        "message": f"{settings.api_key_env} is configured" if api_key_configured else f"{settings.api_key_env} is not configured",
        "value": {
            "env_name": settings.api_key_env,
            "configured": api_key_configured,
            "source": _source_value(api_key_source),
            "secret": True,
        },
    })

    checks.append({
        "name": "base_url",
        "status": "ok",
        "message": _base_url_message(settings),
        "value": {
            "base_url": settings.base_url,
            "endpoint_url": _provider_endpoint_url(settings),
            "responses_url": resolve_responses_url(settings.base_url)
            if _provider_api_kind(settings) == "responses"
            else None,
            "chat_completions_url": resolve_chat_completions_url(settings.base_url)
            if _provider_api_kind(settings) == "chat_completions"
            else None,
        },
    })
    checks.append({
        "name": "limits",
        "status": "ok",
        "message": "provider timeout and output token limits are bounded",
        "value": {
            "timeout_seconds": int(settings.timeout_seconds),
            "max_output_tokens": int(settings.max_output_tokens),
        },
    })
    return checks


def _provider_check(value: str, *, required: bool) -> dict[str, Any]:
    text = str(value or "").strip().lower()
    if not text:
        if not required:
            return {
                "name": "provider",
                "status": "skipped",
                "message": "assistant.llm.provider is not set because planner LLM is disabled",
            }
        return {
            "name": "provider",
            "status": "error",
            "message": "assistant.llm.provider is required when LLM is enabled",
        }
    if not is_supported_llm_provider(text):
        return {
            "name": "provider",
            "status": "error",
            "message": f"assistant.llm.provider must be one of: {', '.join(supported_llm_providers())}",
            "value": text,
        }
    return {
        "name": "provider",
        "status": "ok",
        "message": "assistant.llm.provider is configured",
        "value": text,
    }


def _required_text_check(name: str, value: str, *, expected: str | None = None, required: bool = True) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        if not required:
            return {
                "name": name,
                "status": "skipped",
                "message": f"assistant.llm.{name} is not set because planner LLM is disabled",
            }
        return {
            "name": name,
            "status": "error",
            "message": f"assistant.llm.{name} is required when LLM is enabled",
        }
    if expected is not None and text != expected:
        return {
            "name": name,
            "status": "error",
            "message": f"assistant.llm.{name} must be {expected}",
            "value": text,
        }
    return {
        "name": name,
        "status": "ok",
        "message": f"assistant.llm.{name} is configured",
        "value": text,
    }


def _provider_api_kind(settings: LlmTranslatorSettings) -> str:
    provider = normalize_llm_provider(settings.provider) or "openai"
    return provider_api_kind(provider)


def _provider_endpoint_url(settings: LlmTranslatorSettings) -> str:
    return provider_endpoint_url(settings)


def _base_url_message(settings: LlmTranslatorSettings) -> str:
    if _provider_api_kind(settings) == "chat_completions":
        return "using default DeepSeek Chat Completions API" if not settings.base_url else "using configured Chat Completions API"
    return "using default OpenAI Responses API" if not settings.base_url else "using configured OpenAI Responses API"


def _live_probe_check(
    *,
    runtime_settings: AssistantSettings,
    effective_env: dict[str, str],
    live: bool,
    live_text: str,
    create_response_fn: CreateStructuredResponseFn | None,
) -> dict[str, Any]:
    if not live:
        return {
            "name": "live_probe",
            "status": "skipped",
            "message": "provider call skipped; pass --live to run a read-only planner probe",
        }
    result = plan_read_only_tools(
        str(live_text or DEFAULT_LIVE_PROBE_TEXT),
        runtime_settings,
        conversation_context=None,
        environ=effective_env,
        create_response_fn=create_response_fn,
    )
    if result.error is not None:
        return {
            "name": "live_probe",
            "status": "error",
            "message": result.error.message,
            "value": {
                "code": result.error.code,
                "trace": dict(result.trace),
            },
        }
    return {
        "name": "live_probe",
        "status": "ok" if result.plan is not None else "error",
        "message": "provider returned a valid planner plan" if result.plan is not None else "provider did not return a planner plan",
        "value": {
            "trace": dict(result.trace),
            "plan": result.plan.public_payload() if result.plan is not None else None,
        },
    }


def _capability_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    capabilities = list(manifest.get("capabilities") or [])
    executable = list(manifest.get("llm_executable_intents") or [])
    non_executable = [
        str(item.get("capability_id") or "")
        for item in capabilities
        if item.get("llm_visible") and not item.get("llm_executable")
    ]
    return {
        "schema_version": manifest.get("schema_version"),
        "visible_count": len(capabilities),
        "llm_executable_count": len(executable),
        "llm_executable_intents": executable,
        "known_non_executable_count": len([item for item in non_executable if item]),
        "known_non_executable_intents": [item for item in non_executable if item],
    }


def _source_value(source: Any) -> str | None:
    if source is None:
        return None
    public_value: Callable[[], str] | None = getattr(source, "public_value", None)
    if callable(public_value):
        return public_value()
    return str(source)
