from __future__ import annotations

from src.application.copilot.contracts import AppResult, CopilotRequest, ExecutionContract, new_id
from src.application.copilot.scene import GENERAL_SCENE


def prepare_contract(request: CopilotRequest, *, reference_year: int) -> ExecutionContract | AppResult:
    message = request.user_message.strip()
    if not message:
        return AppResult(
            status="needs_clarification",
            user_response="请提供要查询或分析的问题。",
            request_id=request.request_id,
        )
    scope = request.explicit_scope
    config_key = _text(scope.config_key, lower=True)
    symbol = _text(scope.symbol, upper=True)
    month = _text(scope.month)
    messages = [dict(item) for item in request.context_messages]
    messages.append({"role": "user", "content": message})
    fixture_id = None
    if request.execution_environment == "eval":
        raw_fixture = request.debug_overrides.get("fixture_id")
        fixture_id = str(raw_fixture).strip() if raw_fixture is not None else None
    return ExecutionContract(
        contract_id=new_id("contract"),
        request_id=request.request_id,
        scene_name=GENERAL_SCENE,
        execution_environment=request.execution_environment,
        input={
            "user_message": message,
            "config_key": config_key,
            "symbol": symbol,
            "month": month,
            "reference_year": int(reference_year),
            "messages": messages,
            "fixture_id": fixture_id,
        },
        policy={
            "read_only": True,
        },
        decision_trace={
            "scope_sources": {
                key: f"explicit_scope.{key}"
                for key, value in (("config_key", config_key), ("symbol", symbol), ("month", month))
                if value
            },
            "selected_scene": GENERAL_SCENE,
            "selection_reason": "entry_surface_default",
        },
    )


def _text(value: object, *, lower: bool = False, upper: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if lower:
        return text.lower()
    if upper:
        return text.upper()
    return text


__all__ = ["prepare_contract"]
