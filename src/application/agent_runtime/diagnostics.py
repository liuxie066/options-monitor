from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.application.agent_runtime.llm_translator import CreateStructuredResponseFn, translate_inbound_intent
from src.application.agent_runtime.settings import AgentRuntimeSettings, LlmTranslatorSettings
from src.application.agent_tool_config import load_runtime_config
from src.application.config_validator import validate_config
from src.application.settings import build_effective_env
from src.infrastructure.openai_chat_completions import resolve_chat_completions_url
from src.infrastructure.openai_responses import resolve_responses_url


DEFAULT_LIVE_PROBE_TEXT = "状态"


def check_llm_translator(
    *,
    repo_root: str | Path,
    config_key: str | None = None,
    config_path: str | Path | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
    live: bool = False,
    live_text: str = DEFAULT_LIVE_PROBE_TEXT,
    create_response_fn: CreateStructuredResponseFn | None = None,
) -> dict[str, Any]:
    path, cfg = load_runtime_config(config_key=config_key, config_path=config_path)
    settings = AgentRuntimeSettings.from_runtime_config(cfg).llm
    effective_env = build_effective_env(
        repo_root=repo_root,
        env_file=env_file,
        include_local_env_file=include_local_env_file,
    )

    checks: list[dict[str, Any]] = []
    validation_ok = _append_runtime_config_check(checks, cfg=cfg)
    checks.extend(_config_checks(settings, effective_env=effective_env))

    live_probe = _live_probe_check(
        settings=settings,
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
            "config_key": str(config_key or "").strip().lower() or None,
            "config_path": str(path),
        },
        "env": {
            "env_file": str(effective_env.env_file) if effective_env.env_file is not None else None,
            "env_file_loaded": bool(effective_env.env_file_loaded),
            "warnings": list(effective_env.warnings),
        },
        "llm": {
            **settings.public_payload(),
            "endpoint_url": _provider_endpoint_url(settings),
            "responses_url": resolve_responses_url(settings.base_url) if _provider_name(settings) == "openai" else None,
            "chat_completions_url": resolve_chat_completions_url(settings.base_url)
            if _provider_name(settings) == "deepseek"
            else None,
            "api_key_configured": bool(effective_env.get(settings.api_key_env)),
            "api_key_source": _source_value(effective_env.source_of(settings.api_key_env)),
        },
        "checks": checks,
    }


def _append_runtime_config_check(checks: list[dict[str, Any]], *, cfg: dict[str, Any]) -> bool:
    try:
        validate_config(dict(cfg))
    except SystemExit as exc:
        checks.append({
            "name": "runtime_config",
            "status": "error",
            "message": str(exc),
        })
        return False
    checks.append({
        "name": "runtime_config",
        "status": "ok",
        "message": "runtime config validates",
    })
    return True


def _config_checks(settings: LlmTranslatorSettings, *, effective_env: Any) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append({
        "name": "enabled",
        "status": "ok" if settings.enabled else "warn",
        "message": "agent.llm.enabled is true" if settings.enabled else "agent.llm.enabled is false; translator is inactive",
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
            "responses_url": resolve_responses_url(settings.base_url) if _provider_name(settings) == "openai" else None,
            "chat_completions_url": resolve_chat_completions_url(settings.base_url)
            if _provider_name(settings) == "deepseek"
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
                "message": "agent.llm.provider is not set because LLM is disabled",
            }
        return {
            "name": "provider",
            "status": "error",
            "message": "agent.llm.provider is required when LLM is enabled",
        }
    if text not in {"openai", "deepseek"}:
        return {
            "name": "provider",
            "status": "error",
            "message": "agent.llm.provider must be one of: openai, deepseek",
            "value": text,
        }
    return {
        "name": "provider",
        "status": "ok",
        "message": "agent.llm.provider is configured",
        "value": text,
    }


def _required_text_check(name: str, value: str, *, expected: str | None = None, required: bool = True) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        if not required:
            return {
                "name": name,
                "status": "skipped",
                "message": f"agent.llm.{name} is not set because LLM is disabled",
            }
        return {
            "name": name,
            "status": "error",
            "message": f"agent.llm.{name} is required when LLM is enabled",
        }
    if expected is not None and text != expected:
        return {
            "name": name,
            "status": "error",
            "message": f"agent.llm.{name} must be {expected}",
            "value": text,
        }
    return {
        "name": name,
        "status": "ok",
        "message": f"agent.llm.{name} is configured",
        "value": text,
    }


def _provider_name(settings: LlmTranslatorSettings) -> str:
    return str(settings.provider or "").strip().lower() or "openai"


def _provider_endpoint_url(settings: LlmTranslatorSettings) -> str:
    provider = _provider_name(settings)
    if provider == "deepseek":
        return resolve_chat_completions_url(settings.base_url)
    return resolve_responses_url(settings.base_url)


def _base_url_message(settings: LlmTranslatorSettings) -> str:
    provider = _provider_name(settings)
    if provider == "deepseek":
        return "using default DeepSeek Chat Completions API" if not settings.base_url else "using configured Chat Completions API"
    return "using default OpenAI Responses API" if not settings.base_url else "using configured OpenAI Responses API"


def _live_probe_check(
    *,
    settings: LlmTranslatorSettings,
    effective_env: dict[str, str],
    live: bool,
    live_text: str,
    create_response_fn: CreateStructuredResponseFn | None,
) -> dict[str, Any]:
    if not live:
        return {
            "name": "live_probe",
            "status": "skipped",
            "message": "provider call skipped; pass --live to run a read-only translation probe",
        }
    result = translate_inbound_intent(
        str(live_text or DEFAULT_LIVE_PROBE_TEXT),
        settings=settings,
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
        "status": "ok" if result.intent is not None else "error",
        "message": "provider returned a valid read-only intent" if result.intent is not None else "provider did not return an intent",
        "value": {
            "trace": dict(result.trace),
            "intent": result.intent.public_payload() if result.intent is not None else None,
        },
    }


def _source_value(source: Any) -> str | None:
    if source is None:
        return None
    public_value: Callable[[], str] | None = getattr(source, "public_value", None)
    if callable(public_value):
        return public_value()
    return str(source)
