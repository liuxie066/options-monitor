from __future__ import annotations

from typing import Any

from src.application.assistant.contracts import ActionResult, ObservationResponse, PerceptionResult, ReasoningResolution
from src.application.assistant.renderer import render_inbound_text


def build_observation(
    *,
    perception: PerceptionResult | None,
    resolution: ReasoningResolution | None,
    action: ActionResult,
) -> ObservationResponse:
    error_code = _error_code(action.error)
    if action.response_text:
        return ObservationResponse(
            response_text=action.response_text,
            ok=bool(action.ok),
            status=resolution.status if resolution else ("ok" if action.ok else "failed"),
            error_code=error_code,
        )
    if action.result:
        return ObservationResponse(
            response_text=render_inbound_text(intent=perception, tool_result=action.result),
            ok=bool(action.ok),
            status=resolution.status if resolution else ("ok" if action.ok else "failed"),
            error_code=error_code or _error_code(action.result.get("error") if isinstance(action.result, dict) else None),
        )
    if action.error:
        return ObservationResponse(
            response_text=render_inbound_text(intent=perception, tool_result=None, error=action.error),
            ok=False,
            status=resolution.status if resolution else "failed",
            error_code=error_code,
        )
    return ObservationResponse(
        response_text="没有执行结果。",
        ok=bool(action.ok),
        status=resolution.status if resolution else "unknown",
        error_code=error_code,
    )


def _error_code(error: Any) -> str | None:
    if isinstance(error, dict):
        return str(error.get("code") or "") or None
    return None


__all__ = ["build_observation"]
