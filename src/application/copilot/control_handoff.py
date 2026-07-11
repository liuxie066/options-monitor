from __future__ import annotations

from typing import Any

CONTROL_PREVIEW_TOOL = "request_control_preview"


def control_preview_tool_description(specs: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    capability_lines = [
        f"{spec['intent_name']}: {spec.get('summary') or spec.get('display_name')}; "
        f"arguments={', '.join(spec.get('arguments') or ()) or 'none'}"
        for spec in specs
    ]
    return {
        "name": CONTROL_PREVIEW_TOOL,
        "description": (
            "Request a deterministic preview for a user-requested state change. "
            "This never applies a write and must not be used for confirm/cancel replies. Available capabilities: "
            + " | ".join(capability_lines)
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent_name": {"type": "string", "enum": [spec["intent_name"] for spec in specs]},
                "arguments": {"type": "object"},
            },
            "required": ["intent_name", "arguments"],
            "additionalProperties": False,
        },
    }


def build_control_preview_request(
    arguments: dict[str, Any],
    *,
    user_message: str,
    specs: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any] | None, str | None]:
    intent_name = str(arguments.get("intent_name") or "").strip()
    spec = next((item for item in specs if item.get("intent_name") == intent_name), None)
    if spec is None:
        return None, "intent is not an allowed preview capability"
    raw_arguments = arguments.get("arguments")
    if not isinstance(raw_arguments, dict):
        return None, "arguments must be an object"
    allowed_arguments = tuple(str(item) for item in spec.get("arguments") or ())
    unknown = sorted(str(key) for key in raw_arguments if str(key) not in allowed_arguments)
    if unknown:
        return None, f"unsupported arguments for {intent_name}: {', '.join(unknown)}"
    payload = dict(raw_arguments)
    if "raw_text" in allowed_arguments and not str(payload.get("raw_text") or "").strip():
        payload["raw_text"] = str(user_message or "").strip()
    return {
        "intent_name": intent_name,
        "arguments": payload,
        "source": "copilot_control_preview",
        "confidence": 1.0,
    }, None


__all__ = [
    "CONTROL_PREVIEW_TOOL",
    "build_control_preview_request",
    "control_preview_tool_description",
]
