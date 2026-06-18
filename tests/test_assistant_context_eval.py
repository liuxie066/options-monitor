from __future__ import annotations

from pathlib import Path

from src.application.assistant.context_eval import format_context_eval_text, run_context_eval_suite


FIXTURE_DIR = Path(__file__).parent / "fixtures"
AGENT_EVAL_FIXTURE = FIXTURE_DIR / "assistant_agent_eval.jsonl"
PROJECTION_FIXTURE = FIXTURE_DIR / "assistant_context_projection.jsonl"


def test_context_eval_projection_mode_runs_projection_fixture() -> None:
    report = run_context_eval_suite(fixture_path=PROJECTION_FIXTURE, mode="projection")
    summary = report["summary"]
    results = {item["id"]: item for item in report["results"]}

    assert summary["mode"] == "projection"
    assert summary["ok"] is True
    assert summary["total"] == 1
    assert summary["failed"] == 0

    result = results["projection_keeps_recent_tool_ref"]
    actual = result["actual"]
    projection = actual["context_projection"]
    assert projection["recent_turn_count"] >= 2
    assert projection["recent_successful_tool_count"] == 2
    assert projection["evidence_ref_count"] == 2
    assert actual["safe_slots"]["account"] == ["lx", "sy"]
    assert actual["safe_slots"]["month"] == ["2026-06"]

    text = format_context_eval_text(report)
    assert "assistant context eval: 1/1 passed" in text
    assert "mode=projection" in text
    assert "refs=2" in text


def test_context_eval_deferred_modes_have_explicit_empty_reports() -> None:
    for mode in ("validation", "scenarios"):
        report = run_context_eval_suite(fixture_path=AGENT_EVAL_FIXTURE, mode=mode)
        summary = report["summary"]

        assert summary["mode"] == mode
        assert summary["ok"] is True
        assert summary["total"] == 0
        assert summary["failed"] == 0
        assert summary["empty"] is True
