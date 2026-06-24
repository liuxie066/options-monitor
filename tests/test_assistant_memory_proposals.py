from __future__ import annotations

from pathlib import Path

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.memory import load_assistant_memory_context
from src.application.assistant.memory_proposals import (
    accept_memory_proposal,
    list_memory_proposals,
    reject_memory_proposal,
    save_memory_proposal,
    suggest_memory_proposals_from_text,
)


def test_memory_proposal_accept_writes_loadable_memory(tmp_path: Path) -> None:
    memory_dir = tmp_path / "assistant_memory"

    proposed = save_memory_proposal(
        memory_dir=memory_dir,
        proposal_id="proposal_parameter_tuning",
        memory_id="parameter-tuning",
        memory_type="parameter_tuning_preference",
        title="参数调优偏好",
        summary="用户希望先看候选过滤证据。",
        content="优化参数时先看 replay、候选过滤和拒绝原因。",
        tags=["参数", "候选"],
        source_turn="turn_123",
        why_remember="稳定调参协作偏好",
        risk_check="不包含价格、持仓、配置值或权限。",
    )

    assert proposed["write_applied"] is True
    assert proposed["proposal"]["status"] == "proposed"
    listed = list_memory_proposals(memory_dir=memory_dir)
    assert listed["proposal_count"] == 1
    assert listed["proposals"][0]["proposal_id"] == "proposal_parameter_tuning"

    accepted = accept_memory_proposal(
        memory_dir=memory_dir,
        proposal_id="proposal_parameter_tuning",
    )

    assert accepted["write_applied"] is True
    assert accepted["proposal"]["status"] == "accepted"
    memory_path = memory_dir / "parameter-tuning.md"
    assert memory_path.exists()
    assert "status: accepted" in memory_path.read_text(encoding="utf-8")

    memory = load_assistant_memory_context(path=memory_dir, query="参数")
    assert memory["provided"] is True
    assert memory["memories"][0]["memory_id"] == "parameter-tuning"
    assert memory["memories"][0]["type"] == "parameter_tuning_preference"


def test_memory_proposal_reject_only_updates_proposal_status(tmp_path: Path) -> None:
    memory_dir = tmp_path / "assistant_memory"
    save_memory_proposal(
        memory_dir=memory_dir,
        proposal_id="proposal_workflow",
        memory_id="workflow-pattern",
        memory_type="workflow_pattern",
        title="工作流偏好",
        summary="用户偏好先读 authority path。",
        content="诊断前先确认当前 authority path。",
    )

    rejected = reject_memory_proposal(
        memory_dir=memory_dir,
        proposal_id="proposal_workflow",
        reason="too broad",
    )

    assert rejected["proposal"]["status"] == "rejected"
    assert rejected["proposal"]["rejection_reason"] == "too broad"
    assert not (memory_dir / "workflow-pattern.md").exists()
    listed = list_memory_proposals(memory_dir=memory_dir, status="rejected")
    assert listed["proposal_count"] == 1


def test_memory_proposal_rejects_sensitive_content(tmp_path: Path) -> None:
    with pytest.raises(AgentToolError) as exc:
        save_memory_proposal(
            memory_dir=tmp_path / "assistant_memory",
            proposal_id="proposal_secret",
            memory_id="secret",
            memory_type="workflow_pattern",
            title="secret",
            summary="should fail",
            content="webhook: https://example.invalid/secret",
        )

    assert exc.value.code == "INPUT_ERROR"
    assert "sensitive" in exc.value.message


def test_memory_proposal_rejects_sensitive_tags(tmp_path: Path) -> None:
    with pytest.raises(AgentToolError) as exc:
        save_memory_proposal(
            memory_dir=tmp_path / "assistant_memory",
            proposal_id="proposal_secret_tag",
            memory_id="secret-tag",
            memory_type="workflow_pattern",
            title="workflow",
            summary="should fail",
            content="safe content",
            tags=["workflow", "webhook: https://example.invalid/secret"],
        )

    assert exc.value.code == "INPUT_ERROR"
    assert exc.value.details["field"] == "tags"


def test_memory_suggest_creates_proposal_only_from_explicit_preference(tmp_path: Path) -> None:
    memory_dir = tmp_path / "assistant_memory"

    result = suggest_memory_proposals_from_text(
        memory_dir=memory_dir,
        source_turn="turn_456",
        text="请记住：优化参数时先看 replay、候选过滤和拒绝原因。",
    )

    assert result["action"] == "suggest"
    assert result["write_applied"] is True
    assert result["suggestion_count"] == 1
    assert result["proposal_count"] == 1
    proposal = result["proposals"][0]
    assert proposal["status"] == "proposed"
    assert proposal["type"] == "parameter_tuning_preference"
    assert proposal["source_turn"] == "turn_456"
    assert "replay" in proposal["content"]
    assert not (memory_dir / "parameter-tuning-preference.md").exists()
    listed = list_memory_proposals(memory_dir=memory_dir)
    assert listed["proposal_count"] == 1


def test_memory_suggest_ignores_current_market_or_runtime_facts(tmp_path: Path) -> None:
    result = suggest_memory_proposals_from_text(
        memory_dir=tmp_path / "assistant_memory",
        text="请记住：今天 NVDA 当前价格是 180，筛选通过。",
    )

    assert result["write_applied"] is False
    assert result["suggestion_count"] == 0
    assert result["proposal_count"] == 0
    assert result["skipped"] == [{"reason": "runtime_or_market_fact"}]


def test_memory_suggest_distinguishes_missing_signal_from_explicit_short_text(tmp_path: Path) -> None:
    memory_dir = tmp_path / "assistant_memory"

    missing_signal = suggest_memory_proposals_from_text(memory_dir=memory_dir, text="短", write=False)
    explicit_short = suggest_memory_proposals_from_text(memory_dir=memory_dir, text="记住", write=False)

    assert missing_signal["skipped"] == [{"reason": "missing_explicit_memory_signal"}]
    assert explicit_short["skipped"] == [{"reason": "too_short"}]


def test_memory_suggest_preview_does_not_write_proposal(tmp_path: Path) -> None:
    memory_dir = tmp_path / "assistant_memory"

    result = suggest_memory_proposals_from_text(
        memory_dir=memory_dir,
        text="请记住：调参时先确认候选过滤证据。",
        write=False,
    )

    assert result["dry_run"] is True
    assert result["write_applied"] is False
    assert result["proposal_count"] == 1
    assert result["proposals"][0]["type"] == "parameter_tuning_preference"
    assert list((memory_dir / "proposals").glob("*.json")) == []


def test_memory_suggest_ignores_sensitive_material(tmp_path: Path) -> None:
    result = suggest_memory_proposals_from_text(
        memory_dir=tmp_path / "assistant_memory",
        text="请记住：webhook: https://example.invalid/secret",
    )

    assert result["write_applied"] is False
    assert result["proposal_count"] == 0
    assert result["skipped"] == [{"reason": "sensitive_material"}]


def test_memory_proposal_accept_requires_replace_for_existing_memory(tmp_path: Path) -> None:
    memory_dir = tmp_path / "assistant_memory"
    memory_dir.mkdir()
    (memory_dir / "workflow-pattern.md").write_text(
        """\
---
type: workflow_pattern
title: "existing"
summary: "existing"
status: active
---

existing
""",
        encoding="utf-8",
    )
    save_memory_proposal(
        memory_dir=memory_dir,
        proposal_id="proposal_existing",
        memory_id="workflow-pattern",
        memory_type="workflow_pattern",
        title="工作流偏好",
        summary="用户偏好先读 authority path。",
        content="诊断前先确认当前 authority path。",
    )

    with pytest.raises(AgentToolError) as exc:
        accept_memory_proposal(memory_dir=memory_dir, proposal_id="proposal_existing")

    assert exc.value.code == "INPUT_ERROR"
    assert "already exists" in exc.value.message
