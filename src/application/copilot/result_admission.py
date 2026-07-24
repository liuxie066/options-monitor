from __future__ import annotations

import json
import re
from dataclasses import dataclass

from src.application.copilot.contracts import AppResult


VALID_STATUSES = {
    "answered",
    "needs_clarification",
    "insufficient_evidence",
    "cancelled",
    "refused",
    "not_ready",
    "failed",
    "control_requested",
}

TOOL_PROTOCOL_MARKERS = (
    "<tool_calls>",
    "<｜｜DSML｜｜tool_calls>",
    "<｜｜DSML｜｜invoke",
)

_FENCE_LINE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<rest>.*)$")
_WHOLE_CONTAINER = re.compile(
    r"^(?P<fence>`{3,}|~{3,})(?P<label>[A-Za-z0-9_-]+)[ \t]*\n"
    r"(?P<body>.*)\n(?P=fence)[ \t]*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class AdmissionDecision:
    result: AppResult
    rejection_reason: str | None = None


def admit_result(result: AppResult) -> AppResult:
    return admit_result_with_decision(result).result


def admit_result_with_decision(result: AppResult) -> AdmissionDecision:
    response = str(result.user_response or "").strip()
    if result.status not in VALID_STATUSES:
        return _reject(result, "invalid_status")
    if result.status not in {"failed", "control_requested"} and not response:
        return _reject(result, "empty_result")
    if result.status == "answered" and any(marker in response for marker in TOOL_PROTOCOL_MARKERS):
        return _reject(result, "unparsed_tool_protocol")
    if result.status == "answered":
        output_rejection = output_contract_rejection_reason(response)
        if output_rejection:
            return _reject(result, output_rejection)
    return AdmissionDecision(result=result)


def output_contract_rejection_reason(response: str) -> str | None:
    text = str(response or "").strip()
    if not _fences_balanced(text):
        return "unbalanced_code_fence"
    labeled_container = _whole_labeled_container(text)
    lower_text = text.lower()
    mentions_json_container = "```json" in lower_text or "~~~json" in lower_text
    mentions_markdown_container = "```markdown" in lower_text or "~~~markdown" in lower_text
    if labeled_container is not None:
        label, body = labeled_container
        if label == "json":
            return None if _strict_json(body) else "invalid_json_container"
        if label == "markdown":
            return None
    if mentions_json_container:
        return "invalid_json_container"
    if mentions_markdown_container:
        return "invalid_markdown_container"
    if _looks_like_raw_json(text) and not _strict_json(text):
        return "invalid_raw_json"
    return None


def output_contract_matches(mode: str, response: str) -> bool:
    text = str(response or "").strip()
    if output_contract_rejection_reason(text) is not None:
        return False
    if mode == "prose":
        return _whole_labeled_container(text) is None
    if mode == "raw_json":
        return not text.startswith(("```", "~~~")) and _strict_json(text)
    container = _whole_labeled_container(text)
    if container is None:
        return False
    label, body = container
    if mode == "json_fence":
        return label == "json" and _strict_json(body)
    if mode == "markdown_fence":
        return label == "markdown"
    return False


def _fences_balanced(text: str) -> bool:
    opened: tuple[str, int] | None = None
    for line in text.splitlines():
        match = _FENCE_LINE.match(line)
        if match is None:
            continue
        fence = match.group("fence")
        rest = match.group("rest")
        marker = fence[0]
        if opened is None:
            opened = (marker, len(fence))
            continue
        open_marker, open_length = opened
        if marker == open_marker and len(fence) >= open_length and not rest.strip():
            opened = None
    return opened is None


def _whole_labeled_container(text: str) -> tuple[str, str] | None:
    match = _WHOLE_CONTAINER.fullmatch(text)
    if match is None:
        return None
    return match.group("label"), match.group("body")


def _looks_like_raw_json(text: str) -> bool:
    return bool(text) and text[0] in "{["


def _strict_json(text: str) -> bool:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    try:
        json.loads(text, parse_constant=reject_constant)
    except (TypeError, ValueError):
        return False
    return True


def _reject(result: AppResult, reason: str, user_response: str = "") -> AdmissionDecision:
    return AdmissionDecision(result=_failed_result(result, reason, user_response), rejection_reason=reason)


def _failed_result(result: AppResult, reason: str, user_response: str) -> AppResult:
    return AppResult(
        status="failed",
        user_response=user_response or "Copilot 结果未通过结构或安全校验。",
        error={"code": "RESULT_REJECTED", "reason": reason},
        request_id=result.request_id,
        contract_id=result.contract_id,
        run_id=result.run_id,
        events=result.events,
        decision_trace=result.decision_trace,
        ok=False,
    )
