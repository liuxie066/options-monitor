"""Final reply checks; evidence scope/freshness belong to tool projection tests."""
from __future__ import annotations

import pytest

from src.application.bot.contracts import AppResult
from src.application.bot.result_admission import (
    admit_result_with_decision, output_contract_rejection_reason,
)


@pytest.mark.parametrize("text", [
    "概念说明。",
    "本页返回 10 条，仍有更多记录；当前账户状态尚未核实。",
    "历史记录截至 2026-08-22，不代表当前运行状态。",
    '{"status":"unknown","count":null}',
    "```markdown\n说明\n```",
    "字" * 12_001,
])
def test_native_reply_preserves_exact_text_without_injecting_banners(text):
    result = AppResult(status="answered", ok=True, user_response=text)
    decision = admit_result_with_decision(result)
    assert decision.rejection_reason is None
    assert decision.result is result
    assert decision.result.user_response == text


@pytest.mark.parametrize("status", [
    "answered", "needs_clarification", "insufficient_evidence", "cancelled",
    "refused", "not_ready",
])
def test_reply_requires_nonempty_text(status):
    decision = admit_result_with_decision(AppResult(status=status, ok=False, user_response="  "))
    assert decision.rejection_reason == "empty_result"
    assert not decision.result.ok


def test_unknown_status_is_rejected():
    decision = admit_result_with_decision(AppResult(status="invented", ok=True, user_response="说明"))
    assert decision.rejection_reason == "invalid_status"
    assert not decision.result.ok


@pytest.mark.parametrize("marker", [
    "<tool_calls>", "<｜｜DSML｜｜tool_calls>", "<｜｜DSML｜｜invoke",
])
def test_unexecuted_tool_protocol_is_never_delivered(marker):
    decision = admit_result_with_decision(
        AppResult(status="answered", ok=True, user_response=marker + "secret model protocol"))
    assert decision.rejection_reason == "unparsed_tool_protocol"
    assert not decision.result.ok
    assert "secret model protocol" not in decision.result.user_response


@pytest.mark.parametrize("status", ["failed", "control_requested"])
def test_failure_and_control_can_have_no_answer(status):
    result = AppResult(status=status, ok=False, user_response="")
    assert admit_result_with_decision(result).rejection_reason is None


def test_format_quality_remains_evaluable_without_blocking_native_delivery():
    text = "```markdown\n未闭合"
    assert output_contract_rejection_reason(text) == "unbalanced_code_fence"
    assert admit_result_with_decision(
        AppResult(status="answered", ok=True, user_response=text)).rejection_reason is None
