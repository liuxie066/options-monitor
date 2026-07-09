from __future__ import annotations

from typing import Any

from src.application.copilot.contracts import (
    AnswerReport,
    AppResult,
    CopilotRequest,
    ExecutionContract,
    new_id,
)
from src.application.copilot.request_understanding import understand_request
from src.application.copilot.safety_policy import evaluate_safety
from src.application.copilot.scene import (
    capability_hint_definitions,
    missing_capability_scope,
    missing_required_scope,
    reject_scene_override_for_environment,
    select_scene,
)


def prepare_contract(request: CopilotRequest, *, reference_year: int) -> ExecutionContract | AppResult:
    message = request.user_message.strip()
    if not message:
        return _simple_result(request, "needs_clarification", "请提供要分析的问题。")

    understanding = understand_request(
        request,
        capability_hints=capability_hint_definitions(),
        reference_year=reference_year,
    )
    scope = understanding.scope
    safety = evaluate_safety(message)
    if not safety.allowed:
        return _simple_result(
            request,
            "refused",
            "结论：这看起来包含写入、通知、交易、配置或发布意图；Copilot 当前只接受只读问题。",
            ok=True,
            decision_trace={**understanding.trace(), **safety.trace()},
        )

    scene_override = str(request.debug_overrides.get("scene_name") or "").strip()
    if scene_override and request.execution_environment != "eval":
        scene_selection = reject_scene_override_for_environment(
            scene_override=scene_override,
            execution_environment=request.execution_environment,
        )
        return _simple_result(
            request,
            "needs_clarification",
            "只有 eval 允许使用 scene override。",
            decision_trace=_decision_trace(scene_selection.trace(), {**understanding.trace(), **safety.trace()}),
        )
    capabilities = understanding.capabilities
    scene_selection = select_scene(
        capabilities=capabilities,
        execution_environment=request.execution_environment,
        scene_override=scene_override or None,
    )
    selected_scene = scene_selection.selected_scene

    if scene_override and selected_scene:
        scene_capabilities = set(selected_scene.capabilities)
        matched_capabilities = capabilities & scene_capabilities
        capabilities = matched_capabilities or scene_capabilities

    if scene_override and selected_scene is None:
        return _simple_result(
            request,
            "needs_clarification",
            f"不支持 scene：{scene_override}。",
            decision_trace=_decision_trace(scene_selection.trace(), {**understanding.trace(), **safety.trace()}),
        )

    decision_trace = _decision_trace(
        scene_selection.trace(),
        {**understanding.trace(), **safety.trace()},
        override_capabilities=tuple(sorted(capabilities)) if scene_override and selected_scene else (),
    )
    if request.execution_environment == "eval":
        decision_trace["environment"] = "eval"

    if selected_scene:
        capability_missing = missing_capability_scope(selected_scene, capabilities, scope)
        if capability_missing:
            return _simple_result(
                request,
                "needs_clarification",
                "缺少范围：" + "、".join(capability_missing) + "。",
                decision_trace=decision_trace,
            )
        month_candidates = scope.get("month_candidates")
        if "month" in selected_scene.required_scope and isinstance(month_candidates, list) and len(month_candidates) > 1:
            return _simple_result(
                request,
                "needs_clarification",
                "检测到多个月份：" + "、".join(str(item) for item in month_candidates) + "。请先指定一个月份。",
                decision_trace=decision_trace,
            )
        missing = missing_required_scope(selected_scene, scope)
        if missing:
            return _simple_result(
                request,
                "needs_clarification",
                "缺少范围：" + "、".join(missing) + "。",
                decision_trace=decision_trace,
            )
        fixture_input, fixture_error = _fixture_input(request, selected_scene)
        if fixture_error:
            return _simple_result(request, "needs_clarification", fixture_error, decision_trace=decision_trace)
        return _contract(
            request,
            selected_scene,
            scope,
            extra_input=fixture_input,
            decision_trace=decision_trace,
        )

    if request.execution_environment == "channel":
        return _simple_result(
            request,
            "not_ready",
            "结论：Copilot 渠道自由问答尚未开放到可执行场景；本次没有调用工具。",
            decision_trace=decision_trace,
        )

    return _simple_result(
        request,
        "needs_clarification",
        "当前没有匹配到唯一可执行的只读场景；请补充要分析的问题和必要范围。",
        decision_trace=decision_trace,
    )


def _contract(
    request: CopilotRequest,
    scene: Any,
    scope: dict[str, Any],
    *,
    extra_input: dict[str, Any] | None = None,
    decision_trace: dict[str, Any] | None = None,
) -> ExecutionContract:
    contract_input = {
        "user_message": request.user_message,
        "config_key": scope.get("config_key"),
        "symbol": scope.get("symbol"),
        "month": scope.get("month"),
    }
    contract_input.update(extra_input or {})
    return ExecutionContract(
        contract_id=new_id("contract"),
        request_id=request.request_id,
        scene_name=scene.name,
        execution_environment=request.execution_environment,
        input=contract_input,
        policy={"read_only": True},
        decision_trace=decision_trace or {"selected_scene": scene.name},
    )


def _fixture_input(request: CopilotRequest, scene: Any) -> tuple[dict[str, Any] | None, str | None]:
    if request.execution_environment != "eval":
        return None, None
    fixture_id = str(request.debug_overrides.get("fixture_id") or "").strip()
    if not scene.allow_mock_observations:
        return None, f"scene {scene.name} 不支持 eval fixture。"
    if not fixture_id:
        return None, "eval 需要 fixture_id。"
    if fixture_id not in scene.fixture_ids:
        return None, f"未知 eval fixture：{fixture_id}。"
    return {"fixture_id": fixture_id}, None


def _decision_trace(
    scene_trace: dict[str, Any],
    understanding_trace: dict[str, Any],
    *,
    override_capabilities: tuple[str, ...] = (),
) -> dict[str, Any]:
    trace = {**scene_trace, **understanding_trace}
    if override_capabilities:
        trace["requested_capabilities"] = list(override_capabilities)
        trace["capability_sources"] = [
            {"capability": capability, "source": "scene_override", "reason": "debug_scene_override"}
            for capability in override_capabilities
        ]
    return trace


def _simple_result(
    request: CopilotRequest,
    status: str,
    user_response: str,
    *,
    ok: bool = True,
    decision_trace: dict[str, Any] | None = None,
) -> AppResult:
    report = AnswerReport(conclusion=user_response) if user_response else None
    return AppResult(
        status=status,
        answer_report=report,
        request_id=request.request_id,
        decision_trace=decision_trace or {},
        ok=ok,
    )
