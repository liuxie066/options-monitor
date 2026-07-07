from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from typing import Any, Callable, Protocol

from src.application.agent_tool_contracts import AgentToolError, build_error_payload
from src.application.assistant.llm_common import (
    llm_api_key_value,
    missing_llm_config,
    provider_create_response_fn,
    strip_json_code_fence,
    unsupported_llm_provider_error,
)
from src.application.assistant.llm_provider_registry import is_supported_llm_provider, provider_api_kind
from src.application.assistant.settings import AssistantLlmSettings, AssistantSettings
from src.application.assistant_copilot.answer import (
    answer_instructions,
    answer_json_schema,
    normalize_answer_payload,
)
from src.application.assistant_copilot.evidence_ledger import EvidenceLedger
from src.application.assistant_copilot.evidence_plan import (
    COPILOT_READ_TOOLS,
    evidence_plan_from_model,
    evidence_plan_instructions,
    evidence_plan_json_schema,
    read_tool_manifest,
    validate_read_tool,
)
from src.application.assistant_copilot.task_frame import (
    TaskFrame,
    task_frame_from_model,
    task_frame_instructions,
    task_frame_json_schema,
)
from src.application.assistant_copilot.verification import verify_answer
from src.application.tool_execution import execute_tool
from src.infrastructure.openai_chat_completions import extract_chat_completion_text
from src.infrastructure.openai_responses import extract_response_text


ExecuteToolFn = Callable[[str, dict[str, Any]], dict[str, Any]]


class CopilotModelClient(Protocol):
    def complete_json(
        self,
        *,
        stage: str,
        instructions: str,
        input_payload: dict[str, Any],
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class CopilotRuntimeLimits:
    max_tool_calls: int = 6
    max_model_turns: int = 4


class AssistantLlmCopilotModelClient:
    def __init__(
        self,
        settings: AssistantLlmSettings,
        *,
        environ: dict[str, str] | None = None,
        create_response_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self._settings = settings
        self._environ = environ
        self._create_response_fn = create_response_fn

    def complete_json(
        self,
        *,
        stage: str,
        instructions: str,
        input_payload: dict[str, Any],
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        missing = missing_llm_config(self._settings)
        if missing:
            raise AgentToolError(
                code="LLM_UNAVAILABLE",
                message="Copilot v2 requires assistant.llm provider, model, and api_key_env",
                details={"missing": missing, "stage": stage},
            )
        if not is_supported_llm_provider(self._settings.provider):
            raise unsupported_llm_provider_error(self._settings, component="copilot")
        if not self._settings.enabled:
            raise AgentToolError(
                code="LLM_UNAVAILABLE",
                message="assistant LLM config is disabled",
                details={"stage": stage},
            )
        api_key = llm_api_key_value(self._settings, environ=self._environ)
        if not api_key:
            raise AgentToolError(
                code="LLM_UNAVAILABLE",
                message="assistant LLM credential is not configured",
                details={"api_key_env": self._settings.api_key_env, "stage": stage},
            )
        try:
            create_fn = self._create_response_fn or provider_create_response_fn(self._settings.provider)
        except AgentToolError:
            raise
        except Exception as exc:
            raise unsupported_llm_provider_error(self._settings, component="copilot") from exc
        try:
            raw = create_fn(
                api_key=api_key,
                base_url=self._settings.base_url,
                model=self._settings.model,
                input_text=json.dumps(input_payload, ensure_ascii=False, sort_keys=True),
                instructions=instructions,
                json_schema=json_schema,
                timeout=self._settings.timeout_seconds,
                max_output_tokens=self._settings.max_output_tokens,
            )
        except AgentToolError:
            raise
        except Exception as exc:
            raise AgentToolError(
                code="LLM_UNAVAILABLE",
                message=f"Copilot model call failed at stage {stage}: {type(exc).__name__}: {exc}",
                details={"stage": stage, "provider": self._settings.provider, "model": self._settings.model},
            ) from exc
        text = (
            extract_chat_completion_text(raw)
            if provider_api_kind(self._settings.provider) == "chat_completions"
            else extract_response_text(raw)
        )
        try:
            parsed = json.loads(strip_json_code_fence(text))
        except Exception as exc:
            raise AgentToolError(
                code="COPILOT_MODEL_ERROR",
                message=f"Copilot model returned non-JSON output at stage {stage}",
                details={"stage": stage},
            ) from exc
        if not isinstance(parsed, dict):
            raise AgentToolError(code="COPILOT_MODEL_ERROR", message=f"Copilot model output is not an object at stage {stage}")
        return parsed


def run_copilot_task(
    *,
    text: str,
    config_key: str | None = None,
    config_path: str | None = None,
    assistant_settings: AssistantSettings | None = None,
    limits: CopilotRuntimeLimits | None = None,
    dry_run: bool = True,
    now_fn: Callable[[], date] | None = None,
    model_client: CopilotModelClient | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
) -> dict[str, Any]:
    limits = limits or CopilotRuntimeLimits()
    settings = assistant_settings or AssistantSettings()
    client = model_client or AssistantLlmCopilotModelClient(settings.llm)
    task_id = _task_id()
    trace: dict[str, Any] = {
        "schema_version": "om-copilot-session-v1",
        "task_id": task_id,
        "dry_run": bool(dry_run),
        "tool_calls": [],
        "model_turns": [],
    }
    try:
        frame = _build_task_frame(
            client=client,
            text=text,
            task_id=task_id,
            config_key=config_key,
            limits=limits,
            now_fn=now_fn,
            trace=trace,
        )
        ledger = EvidenceLedger()
        if _needs_clarification(frame):
            answer = _clarification_answer(frame)
            return _final_payload(status="needs_clarification", frame=frame, ledger=ledger, answer=answer, trace=trace)

        remaining_tool_calls = max(0, int(limits.max_tool_calls))
        if frame.task_kind in {"analysis", "comparison", "diagnosis"} and remaining_tool_calls > 0:
            _run_tool_step(
                tool_name="analysis_catalog",
                purpose="inspect available analysis views",
                payload=_scope_payload(config_key=config_key, config_path=config_path),
                ledger=ledger,
                trace=trace,
                execute_tool_fn=execute_tool_fn,
            )
            remaining_tool_calls -= 1

        plan = _build_evidence_plan(
            client=client,
            frame=frame,
            ledger=ledger,
            config_key=config_key,
            config_path=config_path,
            max_steps=remaining_tool_calls,
            limits=limits,
            trace=trace,
        )
        for step in plan.steps:
            if remaining_tool_calls <= 0:
                break
            _run_tool_step(
                tool_name=step.tool_name,
                purpose=step.purpose,
                payload=step.payload,
                ledger=ledger,
                trace=trace,
                execute_tool_fn=execute_tool_fn,
            )
            remaining_tool_calls -= 1

        sufficiency = _check_sufficiency(frame, ledger)
        trace["sufficiency"] = sufficiency
        if not sufficiency["ok"]:
            answer = _insufficient_answer(frame, sufficiency)
            return _final_payload(status="insufficient_evidence", frame=frame, ledger=ledger, answer=answer, trace=trace)

        answer = _build_answer(client=client, frame=frame, ledger=ledger, limits=limits, trace=trace)
        verification = verify_answer(answer, frame=frame, ledger=ledger)
        trace["verification"] = verification
        if not verification["ok"]:
            answer = {
                "schema_version": "om-copilot-answer-v1",
                "status": "failed",
                "conclusion": "",
                "findings": [],
                "recommendations": [],
                "missing_data": [],
                "response_text": "Copilot v2 生成的回答没有通过证据校验；本次不输出未验证结论。",
            }
            return _final_payload(status="failed", frame=frame, ledger=ledger, answer=answer, trace=trace, verification=verification)
        return _final_payload(status=answer["status"], frame=frame, ledger=ledger, answer=answer, trace=trace, verification=verification)
    except AgentToolError as err:
        trace["error"] = build_error_payload(err)
        return {
            "schema_version": "om-copilot-run-v1",
            "status": "failed",
            "ok": False,
            "response_text": err.message,
            "error": build_error_payload(err),
            "trace": trace,
        }


def _build_task_frame(
    *,
    client: CopilotModelClient,
    text: str,
    task_id: str,
    config_key: str | None,
    limits: CopilotRuntimeLimits,
    now_fn: Callable[[], date] | None,
    trace: dict[str, Any],
) -> TaskFrame:
    payload = {
        "user_text": text,
        "request_date": (now_fn or date.today)().isoformat(),
        "config_key": config_key,
        "read_only": True,
    }
    _ensure_model_budget(trace=trace, limits=limits, stage="task_frame")
    raw = client.complete_json(
        stage="task_frame",
        instructions=task_frame_instructions(),
        input_payload=payload,
        json_schema=task_frame_json_schema(),
    )
    trace["model_turns"].append({"stage": "task_frame"})
    frame = task_frame_from_model(raw, task_id=task_id, user_text=text, config_key=config_key)
    trace["task_frame"] = frame.public_payload()
    return frame


def _build_evidence_plan(
    *,
    client: CopilotModelClient,
    frame: TaskFrame,
    ledger: EvidenceLedger,
    config_key: str | None,
    config_path: str | None,
    max_steps: int,
    limits: CopilotRuntimeLimits,
    trace: dict[str, Any],
):
    _ensure_model_budget(trace=trace, limits=limits, stage="evidence_plan")
    raw = client.complete_json(
        stage="evidence_plan",
        instructions=evidence_plan_instructions(),
        input_payload={
            "task_frame": frame.public_payload(),
            "read_only_tool_manifest": read_tool_manifest(),
            "evidence_ledger": ledger.compact_for_model(),
            "max_steps": max_steps,
        },
        json_schema=evidence_plan_json_schema(),
    )
    trace["model_turns"].append({"stage": "evidence_plan"})
    plan = evidence_plan_from_model(raw, config_key=config_key, config_path=config_path, max_steps=max_steps)
    trace["evidence_plan_revisions"] = [plan.public_payload()]
    return plan


def _build_answer(
    *,
    client: CopilotModelClient,
    frame: TaskFrame,
    ledger: EvidenceLedger,
    limits: CopilotRuntimeLimits,
    trace: dict[str, Any],
) -> dict[str, Any]:
    _ensure_model_budget(trace=trace, limits=limits, stage="answer")
    raw = client.complete_json(
        stage="answer",
        instructions=answer_instructions(),
        input_payload={
            "task_frame": frame.public_payload(),
            "evidence_ledger": ledger.compact_for_model(),
            "allowed_answer_rules": {
                "must_cite_evidence_refs": True,
                "language": "zh-CN",
                "no_write_execution_claims": True,
            },
        },
        json_schema=answer_json_schema(),
    )
    trace["model_turns"].append({"stage": "answer"})
    answer = normalize_answer_payload(raw)
    trace["answer"] = answer
    return answer


def _ensure_model_budget(*, trace: dict[str, Any], limits: CopilotRuntimeLimits, stage: str) -> None:
    max_turns = max(0, int(limits.max_model_turns))
    turns = trace.get("model_turns") if isinstance(trace.get("model_turns"), list) else []
    if len(turns) >= max_turns:
        raise AgentToolError(
            code="COPILOT_BUDGET_EXCEEDED",
            message=f"Copilot model turn budget exhausted before stage {stage}",
            details={"stage": stage, "max_model_turns": max_turns},
        )


def _run_tool_step(
    *,
    tool_name: str,
    purpose: str,
    payload: dict[str, Any],
    ledger: EvidenceLedger,
    trace: dict[str, Any],
    execute_tool_fn: ExecuteToolFn,
) -> None:
    validate_read_tool(tool_name)
    result = execute_tool_fn(tool_name, dict(payload))
    ref_id = f"obs_{len(ledger.observations) + 1}"
    observation = ledger.add_tool_result(ref_id=ref_id, tool_name=tool_name, purpose=purpose, result=result)
    trace["tool_calls"].append({
        "ref_id": observation.ref_id,
        "tool_name": tool_name,
        "purpose": purpose,
        "payload": dict(payload),
        "ok": observation.ok,
    })


def _check_sufficiency(frame: TaskFrame, ledger: EvidenceLedger) -> dict[str, Any]:
    successful = ledger.successful_evidence_count(include_catalog=False)
    if frame.answer_shape.get("requires_evidence") and successful <= 0:
        return {
            "ok": False,
            "reason": "no_successful_read_evidence",
            "message": "没有成功读取可支持结论的只读证据。",
        }
    if frame.task_kind in {"analysis", "comparison"} and successful <= 0:
        return {
            "ok": False,
            "reason": "analysis_evidence_missing",
            "message": "分析类问题缺少非 catalog 的成功证据。",
        }
    return {"ok": True, "successful_evidence_count": successful}


def _needs_clarification(frame: TaskFrame) -> bool:
    blocked = {"write_intent", "config_write", "notification_write", "broker_action"}
    return any(item in blocked for item in frame.missing_slots)


def _clarification_answer(frame: TaskFrame) -> dict[str, Any]:
    slots = "、".join(frame.missing_slots) if frame.missing_slots else "必要范围"
    return {
        "schema_version": "om-copilot-answer-v1",
        "status": "needs_clarification",
        "conclusion": "",
        "findings": [],
        "recommendations": [],
        "missing_data": list(frame.missing_slots),
        "response_text": f"这个请求需要先明确或改用显式指令处理：{slots}。",
    }


def _insufficient_answer(frame: TaskFrame, sufficiency: dict[str, Any]) -> dict[str, Any]:
    del frame
    message = str(sufficiency.get("message") or "证据不足，无法形成可靠结论。")
    return {
        "schema_version": "om-copilot-answer-v1",
        "status": "insufficient_evidence",
        "conclusion": "",
        "findings": [],
        "recommendations": [],
        "missing_data": [message],
        "response_text": message,
    }


def _final_payload(
    *,
    status: str,
    frame: TaskFrame,
    ledger: EvidenceLedger,
    answer: dict[str, Any],
    trace: dict[str, Any],
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace["evidence_ledger"] = ledger.public_payload()
    trace["final_route"] = "copilot_v2" if status == "answered" else status
    if verification is not None:
        trace["verification"] = verification
    response_text = str(answer.get("response_text") or "").strip()
    return {
        "schema_version": "om-copilot-run-v1",
        "status": status,
        "ok": status not in {"failed"},
        "response_text": response_text,
        "task_frame": frame.public_payload(),
        "evidence_ledger": ledger.public_payload(),
        "answer": answer,
        "verification": verification,
        "trace": trace,
        "allowed_tools": list(COPILOT_READ_TOOLS),
    }


def _scope_payload(*, config_key: str | None, config_path: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if config_key:
        payload["config_key"] = config_key
    if config_path:
        payload["config_path"] = config_path
    return payload


def _task_id() -> str:
    return "copilot_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


__all__ = [
    "AssistantLlmCopilotModelClient",
    "CopilotModelClient",
    "CopilotRuntimeLimits",
    "run_copilot_task",
]
