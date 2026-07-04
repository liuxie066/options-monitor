from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.capability_catalog import llm_capability_manifest
from src.application.assistant.config_loader import load_assistant_config
from src.application.assistant.llm_common import (
    CreateToolCallResponseFn,
    is_supported_llm_provider,
    normalize_llm_provider,
    provider_api_kind,
    provider_endpoint_url,
    supported_llm_providers,
)
from src.application.assistant.agent_loop import create_model_turn_events
from src.application.assistant.settings import AssistantSettings, AssistantLlmSettings
from src.application.settings import build_effective_env
from src.infrastructure.openai_chat_completions import resolve_chat_completions_url
from src.infrastructure.openai_responses import resolve_responses_url


DEFAULT_LIVE_PROBE_TEXT = "状态"


def check_llm_planner(
    *,
    repo_root: str | Path,
    config_path: str | Path | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
    live: bool = False,
    live_text: str = DEFAULT_LIVE_PROBE_TEXT,
    live_texts: list[str] | tuple[str, ...] | None = None,
    live_expected_tools: list[str] | tuple[str, ...] | None = None,
    live_expected_event_types: list[str] | tuple[str, ...] | None = None,
    live_expected_arguments: list[dict[str, Any] | str] | tuple[dict[str, Any] | str, ...] | None = None,
    create_tool_call_response_fn: CreateToolCallResponseFn | None = None,
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
        live_texts=live_texts,
        live_expected_tools=live_expected_tools,
        live_expected_event_types=live_expected_event_types,
        live_expected_arguments=live_expected_arguments,
        create_tool_call_response_fn=create_tool_call_response_fn,
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
    checks.append({
        "name": "assistant_config",
        "status": "ok",
        "message": "assistant config validates",
    })
    return True


def _config_checks(settings: AssistantLlmSettings, *, effective_env: Any) -> list[dict[str, Any]]:
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


def _provider_api_kind(settings: AssistantLlmSettings) -> str:
    provider = normalize_llm_provider(settings.provider) or "openai"
    return provider_api_kind(provider)


def _provider_endpoint_url(settings: AssistantLlmSettings) -> str:
    return provider_endpoint_url(settings)


def _base_url_message(settings: AssistantLlmSettings) -> str:
    if _provider_api_kind(settings) == "chat_completions":
        return "using default DeepSeek Chat Completions API" if not settings.base_url else "using configured Chat Completions API"
    return "using default OpenAI Responses API" if not settings.base_url else "using configured OpenAI Responses API"


def _normalized_live_probe_texts(*, live_text: str, live_texts: list[str] | tuple[str, ...] | None) -> list[str]:
    if live_texts is None:
        return [str(live_text or DEFAULT_LIVE_PROBE_TEXT)]
    texts = [str(item).strip() for item in live_texts if str(item).strip()]
    return texts or [str(live_text or DEFAULT_LIVE_PROBE_TEXT)]


def _normalized_expected_tools(live_expected_tools: list[str] | tuple[str, ...] | None) -> list[str]:
    return [str(item).strip() for item in (live_expected_tools or []) if str(item).strip()]


def _normalized_expected_event_types(live_expected_event_types: list[str] | tuple[str, ...] | None) -> list[str]:
    return [str(item).strip() for item in (live_expected_event_types or []) if str(item).strip()]


def _normalized_expected_arguments(
    live_expected_arguments: list[dict[str, Any] | str] | tuple[dict[str, Any] | str, ...] | None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in live_expected_arguments or []:
        try:
            parsed = json.loads(item) if isinstance(item, str) else item
        except json.JSONDecodeError as exc:
            raise AgentToolError(code="INPUT_ERROR", message="--expect-arguments must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise AgentToolError(code="INPUT_ERROR", message="--expect-arguments must be a JSON object")
        normalized.append(dict(parsed))
    return normalized


def _argument_subset_matches(selected: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(selected.get(key) == value for key, value in expected.items())


def _safe_live_probe_trace(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_live_probe_trace(item)
            for key, item in value.items()
            if "api_key" not in str(key).lower() and str(key).lower() != "raw_provider_payload"
        }
    if isinstance(value, list):
        return [_safe_live_probe_trace(item) for item in value]
    return value


def _live_probe_summary(
    *,
    text: str,
    result: Any,
    expected_tool: str | None = None,
    expected_event_type: str | None = None,
    expected_arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if result.error is not None:
        summary = {
            "text": text,
            "status": "error",
            "message": result.error.message,
            "terminal": result.error.code,
            "selected_tool": None,
            "event_type": None,
            "trace": _safe_live_probe_trace(dict(result.trace)),
            "plan": None,
            "event_plan": None,
            "code": result.error.code,
        }
        if expected_tool:
            summary["expected_tool"] = expected_tool
            summary["tool_match"] = False
        if expected_event_type:
            summary["expected_event_type"] = expected_event_type
            summary["event_type_match"] = False
        if expected_arguments is not None:
            summary["expected_arguments"] = dict(expected_arguments)
            summary["selected_arguments"] = {}
            summary["argument_match"] = False
        return summary
    event_plan = result.event_plan.public_payload() if result.event_plan is not None else None
    steps = event_plan.get("steps") if isinstance(event_plan, dict) else []
    events = event_plan.get("events") if isinstance(event_plan, dict) else []
    first_step = steps[0] if steps and isinstance(steps[0], dict) else {}
    first_event = events[0] if events and isinstance(events[0], dict) else {}
    accepted = event_plan is not None
    summary = {
        "text": text,
        "status": "ok" if accepted else "error",
        "message": "provider returned a valid event-native plan"
        if accepted
        else "provider did not return an event-native plan",
        "terminal": "event_native_plan" if accepted else "missing_event_native_plan",
        "selected_tool": first_step.get("tool_name"),
        "event_type": first_event.get("event_type"),
        "trace": _safe_live_probe_trace(dict(result.trace)),
        "plan": None,
        "event_plan": event_plan,
    }
    if expected_tool:
        summary["expected_tool"] = expected_tool
        summary["tool_match"] = first_step.get("tool_name") == expected_tool
    if expected_event_type:
        summary["expected_event_type"] = expected_event_type
        summary["event_type_match"] = first_event.get("event_type") == expected_event_type
    if expected_arguments is not None:
        selected_arguments = first_step.get("arguments") if isinstance(first_step.get("arguments"), dict) else {}
        summary["expected_arguments"] = dict(expected_arguments)
        summary["selected_arguments"] = dict(selected_arguments)
        summary["argument_match"] = _argument_subset_matches(selected_arguments, expected_arguments)
    return summary


def _live_probe_check(
    *,
    runtime_settings: AssistantSettings,
    effective_env: dict[str, str],
    live: bool,
    live_text: str,
    live_texts: list[str] | tuple[str, ...] | None,
    live_expected_tools: list[str] | tuple[str, ...] | None,
    live_expected_event_types: list[str] | tuple[str, ...] | None,
    live_expected_arguments: list[dict[str, Any] | str] | tuple[dict[str, Any] | str, ...] | None,
    create_tool_call_response_fn: CreateToolCallResponseFn | None,
) -> dict[str, Any]:
    if not live:
        if live_expected_tools or live_expected_event_types or live_expected_arguments:
            raise AgentToolError(
                code="INPUT_ERROR",
                message="pass --live when using live probe expectations",
            )
        return {
            "name": "live_probe",
            "status": "skipped",
            "message": "provider call skipped; pass --live to run a read-only planner probe",
        }

    probes: list[dict[str, Any]] = []
    probe_texts = _normalized_live_probe_texts(live_text=live_text, live_texts=live_texts)
    expected_tools = _normalized_expected_tools(live_expected_tools)
    expected_event_types = _normalized_expected_event_types(live_expected_event_types)
    expected_arguments = _normalized_expected_arguments(live_expected_arguments)
    for label, values in (
        ("tools", expected_tools),
        ("event types", expected_event_types),
        ("arguments", expected_arguments),
    ):
        if len(values) > len(probe_texts):
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"received more expected {label} than live probe texts",
            )
    for index, text in enumerate(probe_texts):
        result = create_model_turn_events(
            text,
            runtime_settings,
            conversation_context=None,
            environ=effective_env,
            create_tool_call_response_fn=create_tool_call_response_fn,
        )
        expected_tool = expected_tools[index] if index < len(expected_tools) else None
        expected_event_type = expected_event_types[index] if index < len(expected_event_types) else None
        expected_argument = expected_arguments[index] if index < len(expected_arguments) else None
        probes.append(
            _live_probe_summary(
                text=text,
                result=result,
                expected_tool=expected_tool,
                expected_event_type=expected_event_type,
                expected_arguments=expected_argument,
            )
        )

    first = probes[0]
    accepted = all(
        probe.get("status") == "ok"
        and probe.get("tool_match", True) is not False
        and probe.get("event_type_match", True) is not False
        and probe.get("argument_match", True) is not False
        for probe in probes
    )
    return {
        "name": "live_probe",
        "status": "ok" if accepted else "error",
        "message": "provider returned a valid event-native plan"
        if len(probes) == 1 and accepted
        else "provider returned valid event-native plans"
        if accepted
        else first.get("message") or "provider did not return a valid event-native plan for every probe",
        "value": {
            "trace": first.get("trace"),
            "plan": None,
            "event_plan": first.get("event_plan"),
            "probe_count": len(probes),
            "probes": probes,
            **({"code": first.get("code")} if first.get("code") else {}),
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
