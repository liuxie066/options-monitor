from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.application.copilot.contracts import AppResult


_SUBMIT_KEYS = {"mode", "status", "answer_markdown", "claims"}
_CLAIM_KEYS = {"text", "kind", "observation_ids", "required_scope"}
_ANSWER_STATUSES = {
    "complete",
    "partial",
    "needs_narrowing",
    "insufficient_evidence",
}
_CLAIM_KINDS = {"current_fact", "historical_fact", "derived_fact", "judgment"}
_SCOPE_RANK = {"point": 0, "requested_page": 1, "full_query": 2}
_MAX_ANSWER_CHARS = 12_000


def admit_submit_answer(
    arguments: dict[str, Any],
    evidence_registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Validate the closed S8 final-answer protocol against request evidence."""

    if not isinstance(arguments, dict) or set(arguments) != _SUBMIT_KEYS:
        return _answer_rejection("answer_schema_invalid", code="INPUT_ERROR")
    mode = arguments.get("mode")
    status = arguments.get("status")
    text = arguments.get("answer_markdown")
    claims = arguments.get("claims")
    if (
        mode not in {"conceptual", "evidence"}
        or status not in _ANSWER_STATUSES
        or not isinstance(text, str)
        or not text.strip()
        or len(text) > _MAX_ANSWER_CHARS
        or not isinstance(claims, list)
    ):
        return _answer_rejection("answer_schema_invalid", code="INPUT_ERROR")
    if output_contract_rejection_reason(text) is not None:
        return _answer_rejection("answer_markdown_invalid", code="INPUT_ERROR")
    if (mode == "conceptual" and claims) or (mode == "evidence" and not claims):
        return _answer_rejection("answer_mode_inconsistent")

    referenced: dict[str, dict[str, Any]] = {}
    for claim in claims:
        rejection = _validate_claim(
            claim,
            answer_status=str(status),
            evidence_registry=evidence_registry,
            referenced=referenced,
        )
        if rejection is not None:
            return rejection

    if status == "complete" and any(
        _evidence_is_incomplete(evidence) for evidence in referenced.values()
    ):
        return _answer_rejection("answer_status_overstates_evidence")
    if any(
        evidence.get("observation_status") == "needs_narrowing"
        for evidence in referenced.values()
    ) and status != "needs_narrowing":
        return _answer_rejection("narrowing_status_required")

    banner = _forced_banner(str(status), referenced)
    approved_text = text + banner
    if (
        len(approved_text) > _MAX_ANSWER_CHARS
        or output_contract_rejection_reason(approved_text) is not None
    ):
        return _answer_rejection("approved_answer_invalid", code="INPUT_ERROR")
    return {
        "observation": {"ok": True, "status": "answer_accepted"},
        "approved_answer": {
            "status": status,
            "text": approved_text,
            "text_sha256": _text_sha256(approved_text),
        },
    }


def _validate_claim(
    claim: Any,
    *,
    answer_status: str,
    evidence_registry: dict[str, dict[str, Any]],
    referenced: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(claim, dict) or set(claim) != _CLAIM_KEYS:
        return _answer_rejection("claim_schema_invalid", code="INPUT_ERROR")
    text = claim.get("text")
    kind = claim.get("kind")
    required_scope = claim.get("required_scope")
    observation_ids = claim.get("observation_ids")
    if (
        not isinstance(text, str)
        or not text.strip()
        or kind not in _CLAIM_KINDS
        or required_scope not in _SCOPE_RANK
        or not isinstance(observation_ids, list)
        or not observation_ids
        or any(not isinstance(item, str) or not item.strip() for item in observation_ids)
        or len(set(observation_ids)) != len(observation_ids)
    ):
        return _answer_rejection("claim_schema_invalid", code="INPUT_ERROR")

    for observation_id in observation_ids:
        evidence = evidence_registry.get(observation_id)
        if evidence is None:
            return _answer_rejection("observation_outside_request")
        if evidence.get("ok") is not True or evidence.get("authorized_read") is not True:
            return _answer_rejection("observation_not_authoritative")
        coverage = evidence.get("coverage")
        if not isinstance(coverage, dict):
            return _answer_rejection("coverage_unknown")
        coverage_status = str(coverage.get("status") or "unknown")
        complete_for = str(coverage.get("complete_for") or "")
        diagnostic_gap = (
            kind == "judgment"
            and answer_status in {"partial", "needs_narrowing", "insufficient_evidence"}
            and coverage_status in {"partial", "unknown"}
        )
        if not diagnostic_gap and (
            coverage_status != "complete"
            or _SCOPE_RANK.get(complete_for, -1) < _SCOPE_RANK[str(required_scope)]
        ):
            return _answer_rejection("claim_scope_not_covered")
        if diagnostic_gap and required_scope != "point":
            return _answer_rejection("diagnostic_claim_must_be_point_scoped")
        if not _freshness_supports(str(kind), evidence.get("freshness")):
            return _answer_rejection("claim_freshness_not_supported")
        referenced[observation_id] = evidence
    return None


def _freshness_supports(kind: str, raw: Any) -> bool:
    freshness = raw if isinstance(raw, dict) else {}
    status = str(freshness.get("status") or "unknown")
    as_of = freshness.get("as_of")
    if kind == "current_fact":
        return status in {"current", "fresh"} and _valid_as_of(as_of)
    if status in {"unknown", "stale"}:
        return kind == "judgment"
    if status == "not_applicable":
        return True
    return status in {"current", "fresh", "historical"} and _valid_as_of(as_of)


def _valid_as_of(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _evidence_is_incomplete(evidence: dict[str, Any]) -> bool:
    coverage = evidence.get("coverage")
    freshness = evidence.get("freshness")
    return (
        not isinstance(coverage, dict)
        or coverage.get("status") != "complete"
        or evidence.get("observation_status") in {"partial", "needs_narrowing"}
        or not isinstance(freshness, dict)
        or freshness.get("status") in {None, "unknown", "stale"}
    )


def _forced_banner(
    status: str,
    referenced: dict[str, dict[str, Any]],
) -> str:
    coverage_by_key: dict[str, dict[str, Any]] = {}
    for evidence in referenced.values():
        coverage = evidence.get("coverage")
        if not isinstance(coverage, dict):
            continue
        key = json.dumps(coverage, ensure_ascii=False, sort_keys=True, default=str)
        coverage_by_key.setdefault(key, coverage)
    coverages = list(coverage_by_key.values())
    scopes = sorted(
        {
            json.dumps(coverage["scope"], ensure_ascii=False, sort_keys=True)
            for coverage in coverages
            if isinstance(coverage.get("scope"), dict) and coverage["scope"]
        }
    )
    scope_text = "；".join(scopes) if scopes else "当前请求"
    unknown = any(coverage.get("status") == "unknown" for coverage in coverages)
    freshness_gap = any(
        not isinstance(evidence.get("freshness"), dict)
        or evidence["freshness"].get("status") in {None, "unknown", "stale"}
        for evidence in referenced.values()
    )
    as_of_values = sorted(
        {
            str(evidence["freshness"]["as_of"])
            for evidence in referenced.values()
            if isinstance(evidence.get("freshness"), dict)
            and _valid_as_of(evidence["freshness"].get("as_of"))
        }
    )
    banners: list[str] = []
    if status == "complete" and as_of_values:
        banners.append(f"> 数据时间：{', '.join(as_of_values)}。")
    elif status == "partial":
        if len(coverages) <= 1:
            coverage = coverages[0] if coverages else {}
            banners.append(
                "> 部分数据："
                + _coverage_banner_detail(coverage, default_scope=scope_text)
                + "。"
            )
        else:
            banners.extend(
                f"> 部分数据（证据 {index}）：{_coverage_banner_detail(coverage)}。"
                for index, coverage in enumerate(coverages, start=1)
            )
    elif status == "needs_narrowing":
        banners.append(
            "> 需要缩小范围：请指定账户、时间、标的或结果范围后重新查询。"
        )
    elif status == "insufficient_evidence":
        banners.append(
            f"> 证据不足：当前证据范围为 {scope_text}，不能支持完整结论。"
        )
    if unknown:
        banners.append("> 完整性未知：现有证据不能支持穷尽性结论。")
    if freshness_gap:
        banners.append("> 时效性不足：部分证据缺少有效数据时间或已经过期。")
    return "" if not banners else "\n\n" + "\n".join(banners)


def _coverage_banner_detail(
    coverage: dict[str, Any],
    *,
    default_scope: str = "当前请求",
) -> str:
    included = _coverage_count(coverage.get("included_count"))
    total = _coverage_count(coverage.get("total_count"))
    omitted = _coverage_count(coverage.get("omitted_count"))
    scope = coverage.get("scope")
    scope_text = (
        json.dumps(scope, ensure_ascii=False, sort_keys=True)
        if isinstance(scope, dict) and scope
        else default_scope
    )
    included_text = (
        f"已纳入 {included} 条" if isinstance(included, int) else "已纳入条数未知"
    )
    return f"{included_text}，总数 {total}，遗漏 {omitted}，范围 {scope_text}"


def _coverage_count(value: Any) -> int | str:
    return value if isinstance(value, int) and not isinstance(value, bool) else "未知"


def _answer_rejection(reason: str, *, code: str = "POLICY_ERROR") -> dict[str, Any]:
    return {
        "observation": {
            "ok": False,
            "status": "rejected",
            "code": code,
            "reason": reason,
            "message": "最终答案未通过证据准入，请按结构化原因修正后重试。",
            "retryable": True,
        }
    }


def _text_sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


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
