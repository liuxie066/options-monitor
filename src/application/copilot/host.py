from __future__ import annotations

import importlib
from dataclasses import replace
from typing import Any, Callable

from src.application.copilot import tools as copilot_tools
from src.application.copilot.agent import ActionDecider
from src.application.copilot.contracts import (
    AnswerReport,
    AppResult,
    ExecutionContract,
    SceneManifest,
    new_id,
)
from src.application.copilot.engine import run_engine
from src.application.copilot.event_store import CopilotEventLog
from src.application.copilot.result_admission import admit_result_with_decision
from src.application.copilot.result_projection import (
    ResultContext,
    observation_event_payload,
    result_from_agent_report,
    result_from_observations,
)
from src.application.copilot.scene import (
    build_scene_manifest,
    scene_policy_rejection_reason,
)


CancellationChecker = Callable[[], bool]
FixtureObservationLoader = Callable[[str | None], list[dict[str, Any]]]
FixtureSynthesisPolicy = Callable[[str | None], bool]
MAX_TRACE_CAPABILITY_CHARS = 80
TRACE_SCOPE_KEYS = ("config_key", "symbol", "month", "fixture_id")


def run_contract(
    contract: ExecutionContract,
    *,
    decide_next_action: ActionDecider | None = None,
    is_cancelled: CancellationChecker | None = None,
    fixture_observations_loader: FixtureObservationLoader | None = None,
    fixture_synthesis_policy: FixtureSynthesisPolicy | None = None,
) -> AppResult:
    run_id = new_id("run")
    event_log = CopilotEventLog(run_id)
    event_log.record("contract_received", _contract_received_payload(contract))
    rejection = _contract_rejection_reason(contract)
    if rejection:
        event_log.record("contract_rejected", {"reason": rejection, "scene": contract.scene_name})
        result = AppResult(
            status="not_ready",
            answer_report=AnswerReport(
                conclusion="结论：Copilot 未执行请求，因为场景合同未通过执行策略校验。",
                missing_data=["contract_rejected"],
            ),
            request_id=contract.request_id,
            contract_id=contract.contract_id,
            run_id=run_id,
            events=event_log.events,
            decision_trace=contract.decision_trace,
        )
        event_log.record_final_result(result)
        return result
    try:
        manifest = _manifest_with_tool_descriptions(build_scene_manifest(contract, run_id))
    except Exception:
        event_log.record("scene_preparation_failed", {"reason": "manifest_error"})
        result = AppResult(
            status="failed",
            answer_report=AnswerReport(
                conclusion="结论：Copilot 未能准备只读执行场景，未调用工具。",
                missing_data=["scene_manifest"],
            ),
            request_id=contract.request_id,
            contract_id=contract.contract_id,
            run_id=run_id,
            events=event_log.events,
            decision_trace=contract.decision_trace,
            ok=False,
        )
        event_log.record_final_result(result)
        return result
    event_log.record(
        "scene_prepared",
        {
            "scene": manifest.scene_name,
            "execution_environment": manifest.execution_environment,
            "allowed_tools": manifest.allowed_tools,
            "requires_answer_synthesis": _manifest_requires_answer_synthesis(manifest),
            "requires_recommendations": _manifest_requires_recommendations(manifest),
            "limits": manifest.limits,
            "output_schema": manifest.output_schema,
            "task_guidance": _task_guidance_trace(manifest.task_guidance),
        },
    )

    try:
        fixture_id = _fixture_id(contract)
        load_fixture_observations = fixture_observations_loader or _default_fixture_observations
        requires_fixture_synthesis = fixture_synthesis_policy or _default_fixture_requires_model_synthesis
        result_context = _result_context(contract, manifest.run_id, event_log, manifest)
        candidate_result = run_engine(
            manifest,
            scene_input=contract.input,
            record_event=event_log.record,
            build_tool_payload=lambda tool_name, scene_input: copilot_tools.build_tool_payload(
                tool_name,
                scene_input,
                static_payloads=manifest.tool_static_payloads,
            ),
            call_read_tool=lambda tool_name, payload: copilot_tools.call_read_tool(
                tool_name,
                payload,
                allowed_tools=tuple(manifest.allowed_tools),
            ),
            compact_observation=copilot_tools.compact_observation,
            build_observation_event=observation_event_payload,
            fixture_observations=load_fixture_observations,
            project_observations=lambda observations, eval_only: result_from_observations(
                result_context,
                observations,
                eval_only=eval_only,
            ),
            project_agent_report=lambda observations, raw_report, required_tools: result_from_agent_report(
                result_context,
                observations,
                raw_report,
                required_tools=required_tools,
            ),
            use_mock_observations=fixture_id is not None,
            fixture_id=fixture_id,
            require_mock_model_synthesis=requires_fixture_synthesis(fixture_id),
            decide_next_action=decide_next_action,
            is_cancelled=is_cancelled,
        )
    except Exception:
        event_log.record("engine_failed", {"reason": "engine_error"})
        result = AppResult(
            status="failed",
            answer_report=AnswerReport(
                conclusion="结论：Copilot 执行循环失败，未继续生成分析结果。",
                missing_data=["engine_failed"],
            ),
            request_id=contract.request_id,
            contract_id=contract.contract_id,
            run_id=run_id,
            events=event_log.events,
            decision_trace=contract.decision_trace,
            ok=False,
        )
        event_log.record_final_result(result)
        return result
    try:
        admission = admit_result_with_decision(candidate_result)
    except Exception:
        event_log.record("result_admission_failed", {"reason": "admission_error"})
        result = AppResult(
            status="failed",
            answer_report=AnswerReport(
                conclusion="结论：Copilot 结果准入校验失败，未返回分析结果。",
                missing_data=["result_admission_failed"],
            ),
            request_id=contract.request_id,
            contract_id=contract.contract_id,
            run_id=run_id,
            events=event_log.events,
            decision_trace=contract.decision_trace,
            ok=False,
        )
        event_log.record_final_result(result)
        return result
    result = admission.result
    if admission.rejection_reason:
        event_log.record(
            "result_admission_rejected",
            {"reason": admission.rejection_reason, "status": result.status},
        )
    event_log.record_final_result(result)
    return result


def _contract_rejection_reason(contract: ExecutionContract) -> str | None:
    return scene_policy_rejection_reason(contract)


def _fixture_id(contract: ExecutionContract) -> str | None:
    value = contract.input.get("fixture_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _default_fixture_observations(fixture_id: str | None) -> list[dict[str, Any]]:
    return _eval_fixture_provider().fixture_observations(fixture_id)


def _default_fixture_requires_model_synthesis(fixture_id: str | None) -> bool:
    return bool(_eval_fixture_provider().fixture_requires_model_synthesis(fixture_id))


def _eval_fixture_provider() -> Any:
    return importlib.import_module("src.application.copilot.eval_fixtures")


def _result_context(
    contract: ExecutionContract,
    run_id: str,
    event_log: CopilotEventLog,
    manifest: SceneManifest,
) -> ResultContext:
    return ResultContext(
        request_id=contract.request_id,
        contract_id=contract.contract_id,
        run_id=run_id,
        events=event_log.events,
        decision_trace=contract.decision_trace,
        execution_environment=manifest.execution_environment,
        requires_answer_synthesis=_manifest_requires_answer_synthesis(manifest),
        requires_recommendations=_manifest_requires_recommendations(manifest),
        answer_dimensions=_manifest_answer_dimensions(manifest),
    )


def _manifest_with_tool_descriptions(manifest: SceneManifest) -> SceneManifest:
    return replace(
        manifest,
        tool_descriptions=copilot_tools.tool_descriptions(
            manifest.allowed_tools,
            static_payloads=manifest.tool_static_payloads,
        ),
    )


def _contract_received_payload(contract: ExecutionContract) -> dict[str, object]:
    return {
        "contract_id": contract.contract_id,
        "request_id": contract.request_id,
        "scene": contract.scene_name,
        "execution_environment": contract.execution_environment,
        "read_only": contract.policy.get("read_only") is True,
        "requested_capabilities": _trace_requested_capabilities(contract),
        "input_scope_keys": _trace_input_scope_keys(contract),
        "has_fixture": _trace_string_present(contract.input.get("fixture_id")),
    }


def _trace_requested_capabilities(contract: ExecutionContract) -> list[str]:
    raw = contract.decision_trace.get("requested_capabilities")
    if not isinstance(raw, (list, tuple, set)):
        return []
    capabilities: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        if not text:
            continue
        if len(text) > MAX_TRACE_CAPABILITY_CHARS:
            text = f"{text[: MAX_TRACE_CAPABILITY_CHARS - 3]}..."
        capabilities.append(text)
    return capabilities


def _trace_input_scope_keys(contract: ExecutionContract) -> list[str]:
    return [
        key
        for key in TRACE_SCOPE_KEYS
        if key != "fixture_id" and _trace_string_present(contract.input.get(key))
    ]


def _trace_string_present(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _task_guidance_trace(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {"instruction_count": 0, "requires_answer_synthesis": False}
    instructions = value.get("instructions")
    answer_dimensions = value.get("answer_dimensions")
    result = {
        "instruction_count": len(instructions) if isinstance(instructions, list) else 0,
        "answer_dimension_count": len(answer_dimensions) if isinstance(answer_dimensions, list) else 0,
        "requires_answer_synthesis": bool(value.get("requires_answer_synthesis") is True),
        "requires_recommendations": bool(value.get("requires_recommendations") is True),
    }
    return result


def _manifest_requires_answer_synthesis(manifest: SceneManifest) -> bool:
    return bool(manifest.task_guidance.get("requires_answer_synthesis") is True)


def _manifest_requires_recommendations(manifest: SceneManifest) -> bool:
    return bool(manifest.task_guidance.get("requires_recommendations") is True)


def _manifest_answer_dimensions(manifest: SceneManifest) -> list[str]:
    value = manifest.task_guidance.get("answer_dimensions")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]
