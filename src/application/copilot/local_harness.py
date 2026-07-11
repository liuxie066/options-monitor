from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from src.application.copilot.agent import ModelRequest, ModelRunner, ModelTurn, ToolCall
from src.application.copilot.contracts import AppResult, CopilotRequest, ExecutionContract
from src.application.copilot.conversation_memory import prepare_contract_with_memory
from src.application.copilot.eval_fixtures import fixture_observations
from src.application.copilot.host import run_contract
from src.application.copilot.host_store import CopilotHostStore
from src.application.copilot.model_client import CopilotModelSettings, build_model_runner
from src.application.copilot.model_config import load_assistant_llm_config, model_api_key_configured
from src.application.copilot.service import prepare_contract


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
    model_runner, model_error = _resolve_model_runner(
        model_config_json=model_config_json,
        assistant_config_path=assistant_config_path,
        model_turn_json=model_turn_json,
        execution_environment=prepared.execution_environment,
    )
    if model_error:
        return _invalid_model_config_result(prepared, model_error)
    if model_runner is not None and host_store is not None and session_key:
        prepared = prepare_contract_with_memory(
            prepared,
            store=host_store,
            session_key=session_key,
            model_runner=model_runner,
        )
    return run_contract(
        prepared,
        model_runner=model_runner,
        fixture_observations_loader=fixture_observations,
        host_store=host_store,
        session_key=session_key,
        control_preview_specs=control_preview_specs,
        resumed_from=resumed_from,
        recovered_observations=recovered_observations,
    )


def _resolve_model_runner(
    *,
    model_config_json: str | None,
    assistant_config_path: str | None,
    model_turn_json: str | None,
    execution_environment: str,
) -> tuple[ModelRunner | None, str | None]:
    configured_sources = sum(bool(str(item or "").strip()) for item in (model_config_json, assistant_config_path))
    if configured_sources > 1:
        return None, "ambiguous_model_source"
    if model_turn_json is not None:
        if configured_sources:
            return None, "model_turn_conflicts_with_model_config"
        return _scripted_model_runner(model_turn_json, execution_environment=execution_environment)
    if model_config_json:
        return _model_runner_from_json(model_config_json)
    return _model_runner_from_assistant_config(assistant_config_path)


def _scripted_model_runner(
    model_turn_json: str,
    *,
    execution_environment: str,
) -> tuple[ModelRunner | None, str | None]:
    if execution_environment != "eval":
        return None, "model_turn_requires_eval"
    try:
        raw = json.loads(model_turn_json)
        items = raw if isinstance(raw, list) else [raw]
        turns = [_model_turn(item) for item in items]
        if not turns:
            raise ValueError("at least one model turn is required")
    except Exception:
        return None, "invalid_model_turn"
    queue = list(turns)

    def _run(_request: ModelRequest) -> ModelTurn:
        if not queue:
            return ModelTurn(text="评估模型脚本没有提供后续回答。")
        return deepcopy(queue.pop(0))

    return _run, None


def _model_turn(raw: Any) -> ModelTurn:
    if not isinstance(raw, dict):
        raise ValueError("model turn must be an object")
    calls: list[ToolCall] = []
    for index, item in enumerate(raw.get("tool_calls") or [], start=1):
        if not isinstance(item, dict):
            raise ValueError("tool call must be an object")
        calls.append(
            ToolCall(
                call_id=str(item.get("call_id") or item.get("id") or f"call_{index}"),
                name=str(item.get("name") or "").strip(),
                arguments=dict(item.get("arguments") or {}),
            )
        )
    return ModelTurn(text=str(raw.get("text") or "").strip(), tool_calls=tuple(calls), raw=dict(raw))


def _model_runner_from_json(model_config_json: str) -> tuple[ModelRunner | None, str | None]:
    try:
        raw = json.loads(model_config_json)
        if not isinstance(raw, dict):
            raise ValueError("model config must be an object")
        ok, key_error = model_api_key_configured(raw)
        if not ok:
            return None, key_error
        return build_model_runner(CopilotModelSettings.from_config(raw)), None
    except Exception:
        return None, "invalid_model_config"


def _model_runner_from_assistant_config(path: str | None) -> tuple[ModelRunner | None, str | None]:
    require_config = bool(str(path or "").strip())
    if not require_config:
        return None, None
    raw, load_error = load_assistant_llm_config(config_path=path, require_config=True)
    if load_error:
        return None, load_error
    if not raw:
        return None, None
    ok, key_error = model_api_key_configured(raw)
    if not ok:
        return None, "assistant_model_api_key_missing" if key_error == "model_api_key_missing" else key_error
    try:
        return build_model_runner(CopilotModelSettings.from_config(raw)), None
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
