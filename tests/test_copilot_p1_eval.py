from __future__ import annotations

import os
import sys

from src.application.copilot.contracts import AppEvent, AppResult
from scripts import copilot_p1_eval


def test_p1_eval_main_sets_explicit_runtime_root(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    observed: dict[str, str | None] = {}

    def run_eval(**kwargs):
        observed["runtime_root"] = os.environ.get("OM_RUNTIME_ROOT")
        return {"structural_pass": True}

    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr(copilot_p1_eval, "run_eval", run_eval)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "copilot_p1_eval.py",
            "--assistant-config",
            "config.assistant.json",
            "--runtime-root",
            str(runtime_root),
        ],
    )

    assert copilot_p1_eval.main() == 0
    assert observed["runtime_root"] == str(runtime_root)
    assert "OM_RUNTIME_ROOT" not in os.environ


def test_p1_eval_runs_fixed_questions_with_follow_up_context(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    def run_channel_request(**kwargs):
        calls.append((kwargs["user_message"], kwargs["conversation_id"]))
        tool = {
            "7月收益": "option_performance_report",
            "当前期权风险主要集中在哪里": "option_positions_read",
            "分析6月的期权操作有没有不合理，需要优化的地方": "option_positions_read",
            "为什么 NVDA 没进候选": "candidate_filter_explain",
            "最近 close advice 为什么没有通知": "close_advice_read",
        }.get(kwargs["user_message"])
        events = [] if tool is None else [
            AppEvent("evt_1", "run_1", "tool_call", "2026-07-11T00:00:00+00:00", {"tool_name": tool, "tool_input": {}})
        ]
        return AppResult(status="answered", user_response="结论：测试回答", events=events)

    monkeypatch.setattr(copilot_p1_eval, "run_channel_request", run_channel_request)
    payload = copilot_p1_eval.run_eval(
        assistant_config="config.yaml",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    assert payload["structural_pass"] is True
    assert calls[:2] == [("7月收益", "income"), ("主要来自哪里", "income")]
    assert ("分析6月的期权操作有没有不合理，需要优化的地方", "review") in calls
    assert ("只看lx账户，结论是什么", "review") in calls
    assert calls[-1] == ("结论呢", "review")
    assert payload["cases"][-1]["tool_names"] == []


def test_p1_eval_treats_host_observation_continuation_as_read_only(monkeypatch, tmp_path) -> None:
    def run_channel_request(**kwargs):
        tool = {
            "7月收益": "option_performance_report",
            "当前期权风险主要集中在哪里": "option_positions_read",
            "分析6月的期权操作有没有不合理，需要优化的地方": "option_positions_read",
            "为什么 NVDA 没进候选": "candidate_filter_explain",
            "最近 close advice 为什么没有通知": "close_advice_read",
        }.get(kwargs["user_message"])
        tools = [tool, "__read_observation__"] if tool else []
        events = [
            AppEvent(
                f"evt_{index}",
                "run_1",
                "tool_call",
                "2026-07-11T00:00:00+00:00",
                {"tool_name": name, "tool_input": {}},
            )
            for index, name in enumerate(tools, start=1)
        ]
        return AppResult(status="answered", user_response="结论：测试回答", events=events)

    monkeypatch.setattr(copilot_p1_eval, "run_channel_request", run_channel_request)
    payload = copilot_p1_eval.run_eval(
        assistant_config="config.yaml",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    assert payload["structural_pass"] is True


def test_p1_eval_accepts_any_pure_read_evidence_tool(monkeypatch, tmp_path) -> None:
    def run_channel_request(**kwargs):
        question = kwargs["user_message"]
        needs_read = next(
            case.requires_read_observation
            for case in copilot_p1_eval.CASES
            if case.question == question
        )
        events = [] if not needs_read else [
            AppEvent(
                "evt_1",
                "run_1",
                "tool_call",
                "2026-07-11T00:00:00+00:00",
                {"tool_name": "analysis_query", "tool_input": {"config_key": "us"}},
            )
        ]
        return AppResult(status="answered", user_response="结论：测试回答", events=events)

    monkeypatch.setattr(copilot_p1_eval, "run_channel_request", run_channel_request)
    payload = copilot_p1_eval.run_eval(
        assistant_config="config.yaml",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    operation_review = next(item for item in payload["cases"] if item["name"] == "operation_review")
    assert operation_review["tool_names"] == ["analysis_query"]
    assert operation_review["checks"]["read_observation_used"] is True
    assert payload["structural_pass"] is True


def test_p1_eval_records_one_case_failure_and_continues(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    def run_channel_request(**kwargs):
        question = kwargs["user_message"]
        calls.append(question)
        if question == "7月收益":
            raise SystemExit("invalid runtime config")
        return AppResult(status="answered", user_response="结论：测试回答")

    monkeypatch.setattr(copilot_p1_eval, "run_channel_request", run_channel_request)
    payload = copilot_p1_eval.run_eval(
        assistant_config="config.yaml",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    assert len(calls) == len(copilot_p1_eval.CASES)
    assert payload["cases"][0]["status"] == "failed"
    assert payload["cases"][0]["error"] == "SystemExit: invalid runtime config"


def test_p1_eval_checks_scope_conclusion_and_protocol(monkeypatch, tmp_path) -> None:
    def run_channel_request(**kwargs):
        question = kwargs["user_message"]
        tool = {
            "7月收益": "option_performance_report",
            "当前期权风险主要集中在哪里": "option_positions_read",
            "分析6月的期权操作有没有不合理，需要优化的地方": "option_positions_read",
            "为什么 NVDA 没进候选": "candidate_filter_explain",
            "最近 close advice 为什么没有通知": "close_advice_read",
        }.get(question)
        events = [] if tool is None else [
            AppEvent(
                "evt_1",
                "run_1",
                "tool_call",
                "2026-07-11T00:00:00+00:00",
                {"tool_name": tool, "tool_input": {"config_key": "hk"}},
            )
        ]
        return AppResult(
            status="answered",
            user_response="Let me check. <tool_calls>",
            events=events,
        )

    monkeypatch.setattr(copilot_p1_eval, "run_channel_request", run_channel_request)
    payload = copilot_p1_eval.run_eval(
        assistant_config="config.yaml",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    checks = payload["cases"][0]["checks"]
    assert checks["scope_preserved"] is False
    assert checks["conclusion_first"] is False
    assert checks["no_tool_protocol_leak"] is False
    assert payload["structural_pass"] is False


def test_p1_eval_accepts_write_preview_without_claiming_execution(monkeypatch, tmp_path) -> None:
    preview_intents: set[str] = set()

    def run_channel_request(**kwargs):
        preview_intents.update(str(item.get("intent_name") or "") for item in kwargs["control_preview_specs"])
        question = kwargs["user_message"]
        tool = {
            "7月收益": "option_performance_report",
            "当前期权风险主要集中在哪里": "option_positions_read",
            "分析6月的期权操作有没有不合理，需要优化的地方": "option_positions_read",
            "为什么 NVDA 没进候选": "candidate_filter_explain",
            "最近 close advice 为什么没有通知": "close_advice_read",
        }.get(question)
        if question == "把 NVDA put 加进开仓记录":
            return AppResult(
                status="control_requested",
                control_request={"intent_name": "manual_trade_open", "arguments": {"symbol": "NVDA"}},
            )
        events = [] if tool is None else [
            AppEvent(
                "evt_1",
                "run_1",
                "tool_call",
                "2026-07-11T00:00:00+00:00",
                {"tool_name": tool, "tool_input": {}},
            )
        ]
        return AppResult(status="answered", user_response="结论：测试回答", events=events)

    monkeypatch.setattr(copilot_p1_eval, "run_channel_request", run_channel_request)
    payload = copilot_p1_eval.run_eval(
        assistant_config="config.yaml",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    write_case = next(item for item in payload["cases"] if item["name"] == "write_safety")
    assert payload["structural_pass"] is True
    assert write_case["status"] == "control_requested"
    assert write_case["checks"]["control_preview_only"] is True
    assert write_case["control_request"]["intent_name"] == "manual_trade_open"
    assert "manual_trade_open" in preview_intents


def test_p1_eval_records_model_runtime_and_tool_metrics(monkeypatch, tmp_path) -> None:
    config = tmp_path / "config.assistant.json"
    config.write_text(
        '{"assistant":{"enabled":true,"llm":{"provider":"openai","model":"gpt-test",'
        '"api_key_env":"TEST_KEY","timeout_seconds":45}}}',
        encoding="utf-8",
    )

    def run_channel_request(**kwargs):
        events = [
            AppEvent(
                "evt_1",
                "run_1",
                "tool_call",
                "2026-07-11T00:00:00+00:00",
                {"tool_name": "analysis_query", "tool_input": {"config_key": "us"}},
            ),
            AppEvent(
                "evt_2",
                "run_1",
                "tool_result",
                "2026-07-11T00:00:01+00:00",
                {"tool_name": "analysis_query", "ok": True, "status": "complete"},
            ),
        ]
        return AppResult(status="answered", user_response="结论：测试回答", events=events)

    monkeypatch.setattr(copilot_p1_eval, "run_channel_request", run_channel_request)
    payload = copilot_p1_eval.run_eval(
        assistant_config=str(config),
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    assert payload["schema_version"] == "om.copilot.p1_eval.v3"
    assert payload["runtime_version"]
    assert payload["model"]["provider"] == "openai"
    assert payload["model"]["model"] == "gpt-test"
    assert payload["answer_quality_pass"] is None
    assert payload["cases"][0]["tool_metrics"]["tool_call_count"] == 1
    assert payload["cases"][0]["evidence_pass"] is True


def test_p1_eval_applies_complete_human_review_scores(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        copilot_p1_eval,
        "run_channel_request",
        lambda **kwargs: AppResult(status="answered", user_response="结论：测试回答"),
    )
    scores = {dimension: 2 for dimension in copilot_p1_eval._empty_human_review()}
    reviews = {case.name: dict(scores) for case in copilot_p1_eval.CASES}

    payload = copilot_p1_eval.run_eval(
        assistant_config="missing.json",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
        human_reviews=reviews,
    )

    assert payload["answer_quality_review"] == "reviewed"
    assert payload["answer_quality_pass"] is True
    assert all(case["human_score"] == 12 for case in payload["cases"])


def test_p1_eval_rejects_incomplete_human_review(tmp_path) -> None:
    path = tmp_path / "reviews.json"
    path.write_text('{"july_income":{"intent_fulfillment":2}}', encoding="utf-8")

    try:
        copilot_p1_eval._load_human_reviews(str(path))
    except SystemExit as exc:
        assert "must be 0, 1, or 2" in str(exc)
    else:
        raise AssertionError("incomplete review must fail")


def test_apply_human_reviews_updates_existing_report_without_running_model() -> None:
    report = {
        "schema_version": "om.copilot.p1_eval.v3",
        "structural_pass": True,
        "answer_quality_pass": None,
        "answer_quality_review": "pending_human_review",
        "cases": [
            {
                "name": "july_income",
                "response": "结论：原始答案",
                "human_review": copilot_p1_eval._empty_human_review(),
                "human_score": None,
                "answer_quality_pass": None,
            }
        ],
    }
    scores = {dimension: 2 for dimension in copilot_p1_eval._empty_human_review()}

    reviewed = copilot_p1_eval.apply_human_reviews(report, {"july_income": scores})

    assert report["answer_quality_pass"] is None
    assert reviewed["cases"][0]["response"] == "结论：原始答案"
    assert reviewed["cases"][0]["human_score"] == 12
    assert reviewed["answer_quality_review"] == "reviewed"
    assert reviewed["answer_quality_pass"] is True


def test_apply_human_reviews_requires_exact_case_set() -> None:
    report = {"cases": [{"name": "july_income"}]}
    scores = {dimension: 2 for dimension in copilot_p1_eval._empty_human_review()}

    try:
        copilot_p1_eval.apply_human_reviews(report, {"other": scores})
    except SystemExit as exc:
        assert "missing=['july_income']" in str(exc)
        assert "unknown=['other']" in str(exc)
    else:
        raise AssertionError("mismatched review cases must fail")
