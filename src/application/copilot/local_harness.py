from __future__ import annotations

import json
from copy import deepcopy

from src.application.copilot.agent import ActionDecider
from src.application.copilot.contracts import AnswerReport, AppResult, CopilotRequest, ExecutionContract
from src.application.copilot.eval_fixtures import fixture_observations, fixture_requires_model_synthesis
from src.application.copilot.host import run_contract
from src.application.copilot.model_config import load_assistant_llm_config, model_api_key_configured
from src.application.copilot.model_client import CopilotModelSettings, build_action_model
from src.application.copilot.model_decider import ModelActionDecider
from src.application.copilot.service import prepare_contract


def run_local_request(
    request: CopilotRequest,
    *,
    reference_year: int,
    model_config_json: str | None = None,
    assistant_config_path: str | None = None,
    model_action_json: str | None = None,
) -> AppResult:
    try:
        prepared = prepare_contract(request, reference_year=reference_year)
    except Exception:
        return AppResult(
            status="failed",
            answer_report=AnswerReport(conclusion="结论：Copilot 未能准备执行合同，未调用工具。"),
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
        model_action_json=model_action_json,
    )


def run_prepared_contract(
    prepared: ExecutionContract,
    *,
    model_config_json: str | None = None,
    assistant_config_path: str | None = None,
    model_action_json: str | None = None,
) -> AppResult:
    action_decider, model_error = _action_decider(
        model_config_json=model_config_json,
        assistant_config_path=assistant_config_path,
        model_action_json=model_action_json,
        execution_environment=prepared.execution_environment,
    )
    if model_error:
        return _invalid_model_config_result(prepared, model_error)
    return run_contract(
        prepared,
        decide_next_action=action_decider,
        fixture_observations_loader=fixture_observations,
        fixture_synthesis_policy=fixture_requires_model_synthesis,
    )


def _action_decider(
    *,
    model_config_json: str | None,
    assistant_config_path: str | None,
    model_action_json: str | None,
    execution_environment: str,
) -> tuple[ActionDecider | None, str | None]:
    if _has_model_config(model_config_json) and _has_model_config(assistant_config_path):
        return None, "ambiguous_model_source"
    action_decider, action_error = _action_decider_from_action_json(
        model_action_json,
        execution_environment=execution_environment,
    )
    if model_action_json or action_error:
        if _has_model_config(model_config_json) or _has_model_config(assistant_config_path):
            return None, "model_action_conflicts_with_model_config"
        return action_decider, action_error
    action_decider, model_error = _action_decider_from_json(model_config_json)
    if model_config_json or model_error:
        return action_decider, model_error
    return _action_decider_from_assistant_config(assistant_config_path)


def _action_decider_from_action_json(
    model_action_json: str | None,
    *,
    execution_environment: str,
) -> tuple[ActionDecider | None, str | None]:
    if model_action_json is None:
        return None, None
    if execution_environment != "eval":
        return None, "model_action_requires_eval"
    if not model_action_json.strip():
        return None, "invalid_model_action"
    try:
        raw = json.loads(model_action_json)
        if not isinstance(raw, dict):
            raise ValueError("model action must be an object")
    except Exception:
        return None, "invalid_model_action"

    def _model(_request: dict) -> dict:
        return deepcopy(raw)

    return ModelActionDecider(_model), None


def _action_decider_from_json(model_config_json: str | None) -> tuple[ActionDecider | None, str | None]:
    if not model_config_json:
        return None, None
    try:
        raw = json.loads(model_config_json)
        if not isinstance(raw, dict):
            raise ValueError("model config must be an object")
        ok, key_error = model_api_key_configured(raw)
        if not ok:
            return None, key_error
        settings = CopilotModelSettings.from_config(raw)
        model = build_action_model(settings)
    except Exception:
        return None, "invalid_model_config"
    return ModelActionDecider(model), None


def _has_model_config(value: str | None) -> bool:
    return bool(str(value or "").strip())


def _action_decider_from_assistant_config(assistant_config_path: str | None) -> tuple[ActionDecider | None, str | None]:
    require_config = assistant_config_path is not None and bool(str(assistant_config_path).strip())
    if not require_config:
        return None, None
    raw, load_error = load_assistant_llm_config(
        config_path=assistant_config_path,
        require_config=require_config,
    )
    if load_error:
        return None, load_error
    if not raw:
        return None, None
    try:
        ok, key_error = model_api_key_configured(raw)
        if not ok:
            if key_error == "model_api_key_missing":
                return None, "assistant_model_api_key_missing"
            return None, key_error
        settings = CopilotModelSettings.from_config(raw)
        model = build_action_model(settings)
    except Exception:
        return None, "invalid_model_config"
    return ModelActionDecider(model), None


def _invalid_model_config_result(contract: ExecutionContract, reason: str) -> AppResult:
    return AppResult(
        status="failed",
        answer_report=AnswerReport(
            conclusion=f"结论：Copilot 模型决策器配置无效：{_model_error_text(reason)}，未调用工具。",
            missing_data=[reason],
        ),
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
        "invalid_model_action": "显式模型 action 无效",
        "invalid_model_config": "模型配置无效",
        "model_api_key_missing": "模型 API key 环境变量未配置",
        "model_action_conflicts_with_model_config": "显式模型 action 与模型配置冲突",
        "model_action_requires_eval": "显式模型 action 只能用于 eval",
    }.get(reason, "模型配置无效")
