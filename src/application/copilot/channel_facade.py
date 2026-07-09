from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from threading import Lock
from typing import Iterable

from src.application.copilot.contracts import (
    AnswerReport,
    AppResult,
    CopilotRequest,
    CopilotScope,
    ExecutionContract,
    new_id,
)
from src.application.copilot.local_harness import run_prepared_contract
from src.application.copilot.model_config import load_assistant_llm_config, model_api_key_configured
from src.application.copilot.rendering import render_user_response
from src.application.copilot.scene import scene_phase_readiness
from src.application.copilot.service import prepare_contract


_RUNNING_CHANNEL_KEYS: set[tuple[str, str]] = set()
_RUNNING_CHANNEL_LOCK = Lock()


def run_channel_request(
    *,
    user_message: str,
    config_key: str | None,
    request_id: str | None = None,
    reference_year: int | None = None,
    assistant_config_path: str | None = None,
    channel_scenes: Iterable[str] | None = None,
    channel: str | None = None,
    sender_id: str | None = None,
    conversation_id: str | None = None,
) -> AppResult:
    year = reference_year or date.today().year
    request = CopilotRequest(
        request_id=request_id or new_id("req"),
        source_entry="channel",
        user_message=user_message,
        explicit_scope=CopilotScope(config_key=config_key),
        execution_environment="channel",
    )
    try:
        prepared = prepare_contract(request, reference_year=year)
    except Exception:
        return render_user_response(_channel_prepare_failed(request))
    if isinstance(prepared, AppResult):
        return render_user_response(prepared)
    if scene_phase_readiness(prepared.scene_name) != "channel_ready":
        return render_user_response(
            _channel_not_ready(prepared, reason="scene_not_channel_ready", message="该场景尚未开放到渠道入口")
        )
    scene_gate = _channel_scene_gate(prepared.scene_name, channel_scenes)
    if scene_gate:
        return render_user_response(
            _channel_not_ready(
                prepared,
                reason=scene_gate,
                message="渠道入口没有显式开放该 Copilot 场景",
            )
        )
    model_gate = _channel_model_gate(assistant_config_path)
    if model_gate:
        return render_user_response(
            _channel_not_ready(
                prepared,
                reason=model_gate,
                message="渠道 Copilot 需要显式可用的 assistant 模型配置",
            )
        )
    with _channel_run_slot(channel=channel, sender_id=sender_id, conversation_id=conversation_id) as entered:
        if not entered:
            return render_user_response(
                _channel_not_ready(
                    prepared,
                    reason="channel_run_already_running",
                    message="同一会话已有 Copilot 分析正在运行",
                )
            )
        try:
            result = run_prepared_contract(
                prepared,
                assistant_config_path=assistant_config_path,
            )
        except Exception:
            result = _channel_run_failed(prepared)
        return render_user_response(result)


def _channel_scene_gate(scene_name: str, channel_scenes: Iterable[str] | None) -> str | None:
    scenes = _normalize_channel_scenes(channel_scenes)
    if not scenes:
        return "channel_scene_allowlist_missing"
    if scene_name not in scenes:
        return "channel_scene_not_enabled"
    return None


def _normalize_channel_scenes(channel_scenes: Iterable[str] | None) -> tuple[str, ...]:
    if channel_scenes is None:
        return ()
    scenes: list[str] = []
    for item in channel_scenes:
        text = str(item or "").strip()
        if text and text not in scenes:
            scenes.append(text)
    return tuple(scenes)


def _channel_model_gate(assistant_config_path: str | None) -> str | None:
    if not str(assistant_config_path or "").strip():
        return "channel_model_config_missing"
    raw, load_error = load_assistant_llm_config(config_path=assistant_config_path, require_config=True)
    if load_error:
        return load_error
    if not raw:
        return "channel_model_profile_missing"
    ok, key_error = model_api_key_configured(raw)
    if not ok:
        if key_error == "model_api_key_missing":
            return "channel_model_api_key_missing"
        return key_error
    return None


@contextmanager
def _channel_run_slot(*, channel: str | None, sender_id: str | None, conversation_id: str | None):
    key = _channel_run_key(channel=channel, sender_id=sender_id, conversation_id=conversation_id)
    with _RUNNING_CHANNEL_LOCK:
        if key in _RUNNING_CHANNEL_KEYS:
            yield False
            return
        _RUNNING_CHANNEL_KEYS.add(key)
    try:
        yield True
    finally:
        with _RUNNING_CHANNEL_LOCK:
            _RUNNING_CHANNEL_KEYS.discard(key)


def _channel_run_key(*, channel: str | None, sender_id: str | None, conversation_id: str | None) -> tuple[str, str]:
    channel_key = str(channel or "unknown").strip().lower() or "unknown"
    conversation_key = str(conversation_id or "").strip() or f"sender:{str(sender_id or '').strip() or 'unknown'}"
    return (channel_key, conversation_key)


def _channel_not_ready(contract: ExecutionContract, *, reason: str, message: str) -> AppResult:
    return AppResult(
        status="not_ready",
        answer_report=AnswerReport(
            conclusion=f"结论：{message}；本次没有调用工具。",
            missing_data=[reason],
        ),
        request_id=contract.request_id,
        contract_id=contract.contract_id,
        decision_trace={**contract.decision_trace, "channel_gate": reason},
    )


def _channel_prepare_failed(request: CopilotRequest) -> AppResult:
    return AppResult(
        status="failed",
        answer_report=AnswerReport(
            conclusion="结论：Copilot 未能准备渠道执行合同，未调用工具。",
            missing_data=["channel_prepare_contract_failed"],
        ),
        request_id=request.request_id,
        decision_trace={"service_error": "channel_prepare_contract_failed"},
        ok=False,
    )


def _channel_run_failed(contract: ExecutionContract) -> AppResult:
    return AppResult(
        status="failed",
        answer_report=AnswerReport(
            conclusion="结论：Copilot 渠道执行失败，未返回分析结果。",
            missing_data=["channel_run_failed"],
        ),
        request_id=contract.request_id,
        contract_id=contract.contract_id,
        decision_trace={**contract.decision_trace, "channel_error": "channel_run_failed"},
        ok=False,
    )


__all__ = ["run_channel_request"]
