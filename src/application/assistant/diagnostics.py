from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import mask_path
from src.application.agent_tool_registry import pure_read_tool_names, pure_read_toolsets
from src.application.assistant.config_loader import load_assistant_config
from src.application.llm_provider_registry import (
    is_supported_llm_provider,
    normalize_llm_provider,
    provider_api_kind,
    provider_requires_api_key,
    supported_llm_providers,
)
from src.application.assistant.settings import AssistantSettings, AssistantLlmSettings
from src.application.settings import build_effective_env
from src.application.secret_store import SecretProvider, resolve_secret_status
from src.infrastructure.openai_chat_completions import resolve_chat_completions_url
from src.infrastructure.openai_responses import resolve_responses_url
from src.infrastructure.secret_store.factory import build_secret_provider


def check_assistant_llm(
    *,
    repo_root: str | Path,
    config_path: str | Path | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
    live: bool = False,
    secret_provider: SecretProvider | None = None,
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
    provider = secret_provider or build_secret_provider(environ=effective_env.values)

    checks: list[dict[str, Any]] = []
    validation_ok = _append_assistant_config_check(checks, cfg=cfg, settings=runtime_settings)
    checks.extend(_config_checks(settings, secret_provider=provider))
    live_probe = _live_probe_check(
        live=bool(live),
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
    requires_api_key = bool(settings.provider) and provider_requires_api_key(settings.provider)
    credential_status = (
        resolve_secret_status(
            settings.credential_name,
            provider=provider,
            legacy_env_name=settings.api_key_env,
        )
        if requires_api_key
        else None
    )

    return {
        "summary": {
            "ok": ok,
            "status": status,
            "enabled": bool(settings.enabled),
            "assistant_copilot_portfolio_enabled": (
                "portfolio" in runtime_settings.enabled_copilot_toolsets
            ),
            "live_checked": bool(live),
            "error_count": sum(1 for item in checks if item.get("status") == "error"),
            "warning_count": sum(1 for item in checks if item.get("status") == "warn"),
        },
        "config": {
            "config_kind": "assistant",
            "config_path": mask_path(path),
            "loaded": bool(cfg),
        },
        "env": {
            "env_file": mask_path(effective_env.env_file) if effective_env.env_file is not None else None,
            "env_file_loaded": bool(effective_env.env_file_loaded),
            "warnings": ["env settings warning" for _item in effective_env.warnings],
        },
        "llm": {
            **settings.public_payload(),
            "endpoint_url": _provider_endpoint_url(settings),
            "responses_url": resolve_responses_url(settings.base_url) if _provider_api_kind(settings) == "responses" else None,
            "chat_completions_url": resolve_chat_completions_url(settings.base_url)
            if _provider_api_kind(settings) == "chat_completions"
            else None,
            "api_key_configured": (
                bool(settings.enabled)
                and (
                    not requires_api_key
                    or bool(credential_status and credential_status.configured)
                )
            ),
            "api_key_source": credential_status.source if credential_status is not None else "not_required",
        },
        "capabilities": _capability_summary(),
        "checks": checks,
    }


def _append_assistant_config_check(checks: list[dict[str, Any]], *, cfg: dict[str, Any], settings: AssistantSettings) -> bool:
    if not cfg:
        checks.append({
            "name": "assistant_config",
            "status": "warn",
            "message": "config.assistant.json not found; using default assistant settings",
        })
        return True
    checks.append({
        "name": "assistant_config",
        "status": "ok",
        "message": "assistant config validates",
    })
    return True


def _config_checks(settings: AssistantLlmSettings, *, secret_provider: SecretProvider) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if secret_provider.backend_name == "env":
        checks.append({
            "name": "secret_backend",
            "status": "warn",
            "message": "OM_SECRET_BACKEND=env and api_key_env are deprecated compatibility surfaces",
        })
    checks.append({
        "name": "enabled",
        "status": "ok" if settings.enabled else "warn",
        "message": "assistant LLM config is enabled" if settings.enabled else "assistant LLM config is disabled",
    })
    checks.append(_provider_check(settings.provider, required=settings.enabled))
    checks.append(_required_text_check("model", settings.model, required=settings.enabled))
    requires_api_key = bool(settings.provider) and provider_requires_api_key(settings.provider)
    checks.append(
        _required_text_check("credential_name", settings.credential_name, required=settings.enabled)
        if requires_api_key
        else {
            "name": "credential_name",
            "status": "skipped",
            "message": "provider does not require a credential",
        }
    )

    credential_status = (
        resolve_secret_status(
            settings.credential_name,
            provider=secret_provider,
            legacy_env_name=settings.api_key_env,
        )
        if requires_api_key
        else None
    )
    api_key_configured = not requires_api_key or bool(credential_status and credential_status.configured)
    checks.append({
        "name": "api_key",
        "status": "ok" if api_key_configured else ("error" if settings.enabled else "warn"),
        "message": (
            "provider does not require an API key"
            if not requires_api_key
            else f"{settings.credential_name} is configured"
            if api_key_configured
            else f"{settings.credential_name} is not configured"
        ),
        "value": {
            "credential_name": settings.credential_name,
            "legacy_env_name": settings.api_key_env,
            "configured": api_key_configured,
            "source": credential_status.source if credential_status is not None else "not_required",
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
                "message": "assistant.llm.provider is not set because assistant LLM config is disabled",
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
                "message": f"assistant.llm.{name} is not set because assistant LLM config is disabled",
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


def _provider_api_kind(settings: AssistantLlmSettings) -> str:
    provider = normalize_llm_provider(settings.provider) or "openai"
    return provider_api_kind(provider)


def _provider_endpoint_url(settings: AssistantLlmSettings) -> str:
    if _provider_api_kind(settings) == "chat_completions":
        return resolve_chat_completions_url(settings.base_url)
    return resolve_responses_url(settings.base_url)


def _base_url_message(settings: AssistantLlmSettings) -> str:
    if _provider_api_kind(settings) == "chat_completions":
        return "using default DeepSeek Chat Completions API" if not settings.base_url else "using configured Chat Completions API"
    return "using default OpenAI Responses API" if not settings.base_url else "using configured OpenAI Responses API"


def _live_probe_check(
    *,
    live: bool,
) -> dict[str, Any]:
    return {
        "name": "live_probe",
        "status": "skipped",
        "message": "provider diagnostics are configuration-only; use Copilot execution for an end-to-end model probe",
        "value": {
            "live_requested": bool(live),
            "probe_count": 0,
            "copilot_runtime": True,
        },
    }


def _capability_summary() -> dict[str, Any]:
    tool_names = sorted(pure_read_tool_names())
    toolsets = {name: list(names) for name, names in sorted(pure_read_toolsets().items())}
    return {
        "schema_version": "om-copilot-tool-summary-v1",
        "pure_read_tool_count": len(tool_names),
        "pure_read_tools": tool_names,
        "toolsets": toolsets,
    }


def _source_value(source: Any) -> str | None:
    if source is None:
        return None
    public_value: Callable[[], str] | None = getattr(source, "public_value", None)
    if callable(public_value):
        return public_value()
    return str(source)
