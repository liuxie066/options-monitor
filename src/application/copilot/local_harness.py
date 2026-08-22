from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.application.copilot.contracts import AppResult, CopilotRequest, ExecutionContract
from src.application.copilot.eval_fixtures import fixture_observations
from src.application.copilot.host import run_contract
from src.application.copilot.host_store import CopilotHostStore
from src.application.copilot.model_config import (
    PiModelSettings,
    _resolve_model_api_key,
    load_assistant_copilot_settings,
    load_assistant_llm_config,
)
from src.application.llm_provider_registry import provider_requires_api_key
from src.application.copilot.service import prepare_contract


_EVAL_MODEL = PiModelSettings(
    provider="deepseek",
    api_kind="openai-completions",
    model="om-eval-fixture",
    base_url="https://example.invalid/v1",
    api_key_env="",
    credential_name="",
    timeout_seconds=90,
    context_window_tokens=24_000,
    max_output_tokens=2_048,
    max_attempts=1,
)
_CHILD_ENV_NAMES = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def run_local_request(
    request: CopilotRequest,
    *,
    reference_year: int,
    model_config_json: str | None = None,
    assistant_config_path: str | None = None,
    model_turn_json: str | None = None,
    host_store: CopilotHostStore | None = None,
    session_key: str | None = None,
) -> AppResult:
    try:
        prepared = prepare_contract(request, reference_year=reference_year)
    except Exception:
        return AppResult(
            status="failed",
            user_response="Copilot 未能准备执行合同。",
            error={"code": "PREPARE_CONTRACT_FAILED"},
            request_id=request.request_id,
            decision_trace={"service_error": "prepare_contract_failed"},
            ok=False,
        )
    if isinstance(prepared, AppResult):
        return prepared
    return run_prepared_contract(
        prepared,
        model_config_json=model_config_json,
        assistant_config_path=assistant_config_path,
        model_turn_json=model_turn_json,
        host_store=host_store,
        session_key=session_key,
    )


def run_prepared_contract(
    prepared: ExecutionContract,
    *,
    model_config_json: str | None = None,
    assistant_config_path: str | None = None,
    model_turn_json: str | None = None,
    host_store: CopilotHostStore | None = None,
    session_key: str | None = None,
    control_preview_specs: tuple[dict[str, Any], ...] = (),
    resumed_from: str | None = None,
    recovered_observations: tuple[dict[str, Any], ...] = (),
) -> AppResult:
    implicit_model_turn = model_turn_json is not None and not str(assistant_config_path or "").strip()
    if implicit_model_turn:
        enabled_optional_toolsets, tool_loading_mode, settings_error = frozenset(), "eager", None
    else:
        enabled_optional_toolsets, tool_loading_mode, settings_error = load_assistant_copilot_settings(
            config_path=assistant_config_path,
            require_config=bool(str(assistant_config_path or "").strip()),
        )
    if settings_error or enabled_optional_toolsets is None:
        return _invalid_model_config_result(prepared, settings_error or "invalid_assistant_config")
    model_settings, debug, model_error = _resolve_pi_model(
        model_config_json=model_config_json,
        assistant_config_path=assistant_config_path,
        model_turn_json=model_turn_json,
        execution_environment=prepared.execution_environment,
    )
    if model_error:
        return _invalid_model_config_result(prepared, model_error)
    try:
        api_key = (
            None
            if debug is not None or model_settings is None
            else _resolve_model_api_key(model_settings)
        )
    except Exception:
        api_key = None
    if (
        model_settings is not None
        and debug is None
        and provider_requires_api_key(model_settings.provider)
        and not api_key
    ):
        reason = (
            "assistant_model_api_key_missing"
            if str(assistant_config_path or "").strip()
            else "model_api_key_missing"
        )
        return _invalid_model_config_result(prepared, reason)
    child_environ = {
        name: os.environ[name]
        for name in _CHILD_ENV_NAMES
        if os.environ.get(name)
    }
    if api_key:
        child_environ["OM_PI_MODEL_API_KEY"] = api_key
    if session_key:
        child_environ["OM_PI_SESSION_DB"] = str(
            (
                host_store.path.with_name("pi_sessions.sqlite3")
                if host_store is not None
                else Path(__file__).resolve().parents[3]
                / "output_shared"
                / "state"
                / "pi_sessions.sqlite3"
            ).absolute()
        )
    return run_contract(
        prepared,
        model_settings=model_settings,
        debug=debug,
        process_environ=child_environ,
        fixture_observations_loader=fixture_observations,
        host_store=host_store,
        session_key=session_key,
        control_preview_specs=control_preview_specs,
        resumed_from=resumed_from,
        recovered_observations=recovered_observations,
        enabled_optional_toolsets=enabled_optional_toolsets,
        tool_loading_mode=tool_loading_mode,
    )


def _resolve_pi_model(
    *,
    model_config_json: str | None,
    assistant_config_path: str | None,
    model_turn_json: str | None,
    execution_environment: str,
) -> tuple[PiModelSettings | None, dict[str, Any] | None, str | None]:
    configured_sources = sum(bool(str(item or "").strip()) for item in (model_config_json, assistant_config_path))
    if configured_sources > 1:
        return None, None, "ambiguous_model_source"
    if model_turn_json is not None:
        if configured_sources:
            return None, None, "model_turn_conflicts_with_model_config"
        return _eval_model(model_turn_json, execution_environment=execution_environment)
    if model_config_json:
        settings, error = _model_from_json(model_config_json)
        return settings, None, error
    settings, error = _model_from_assistant_config(assistant_config_path)
    return settings, None, error


def _eval_model(
    model_turn_json: str,
    *,
    execution_environment: str,
) -> tuple[PiModelSettings | None, dict[str, Any] | None, str | None]:
    if execution_environment != "eval":
        return None, None, "model_turn_requires_eval"
    try:
        raw = json.loads(model_turn_json)
        items = raw if isinstance(raw, list) else [raw]
        turns = [_fixture_turn(item) for item in items]
        if not turns:
            raise ValueError("at least one model turn is required")
    except Exception:
        return None, None, "invalid_model_turn"
    return _EVAL_MODEL, {"fixture_turns": turns, "delay_ms": 0}, None


def _fixture_turn(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("model turn must be an object")
    calls: list[dict[str, Any]] = []
    for index, item in enumerate(raw.get("tool_calls") or [], start=1):
        if not isinstance(item, dict):
            raise ValueError("tool call must be an object")
        tool_name = str(item.get("name") or "").strip()
        arguments = item.get("arguments") or {}
        if not tool_name or not isinstance(arguments, dict):
            raise ValueError("tool call is invalid")
        calls.append(
            {
                "call_id": str(item.get("call_id") or item.get("id") or f"call_{index}"),
                "tool_name": tool_name,
                "arguments": dict(arguments),
            }
        )
    return {"tool_calls": calls} if calls else {"text": str(raw.get("text") or "").strip()}


def _model_from_json(model_config_json: str) -> tuple[PiModelSettings | None, str | None]:
    try:
        raw = json.loads(model_config_json)
        if not isinstance(raw, dict):
            raise ValueError("model config must be an object")
        return PiModelSettings.from_config(raw), None
    except Exception:
        return None, "invalid_model_config"


def _model_from_assistant_config(path: str | None) -> tuple[PiModelSettings | None, str | None]:
    require_config = bool(str(path or "").strip())
    if not require_config:
        return None, None
    raw, load_error = load_assistant_llm_config(config_path=path, require_config=True)
    if load_error:
        return None, load_error
    if not raw:
        return None, None
    try:
        return PiModelSettings.from_config(raw), None
    except Exception:
        return None, "invalid_model_config"


def _invalid_model_config_result(contract: ExecutionContract, reason: str) -> AppResult:
    return AppResult(
        status="failed",
        user_response=f"Copilot 模型配置无效：{_model_error_text(reason)}。",
        error={"code": "MODEL_CONFIG_ERROR", "reason": reason},
        request_id=contract.request_id,
        contract_id=contract.contract_id,
        decision_trace={**contract.decision_trace, "model_config_error": reason},
        ok=False,
    )


def _model_error_text(reason: str) -> str:
    return {
        "ambiguous_model_source": "模型来源不唯一",
        "assistant_config_not_found": "assistant 配置文件不存在",
        "assistant_model_api_key_missing": "模型 API key 环境变量未配置",
        "invalid_assistant_config": "assistant 配置无效",
        "invalid_model_config": "模型配置无效",
        "invalid_model_turn": "评估模型轮次无效",
        "model_api_key_missing": "模型 API key 环境变量未配置",
        "model_turn_conflicts_with_model_config": "评估模型轮次与模型配置冲突",
        "model_turn_requires_eval": "显式模型轮次只允许用于评估环境",
    }.get(reason, reason)


__all__ = ["run_local_request", "run_prepared_contract"]
