from __future__ import annotations

from src.application.copilot.contracts import AppEvent, AppResult
from scripts import copilot_p1_eval


def test_p1_eval_runs_fixed_questions_with_follow_up_context(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    def run_channel_request(**kwargs):
        calls.append((kwargs["user_message"], kwargs["conversation_id"]))
        tool = {
            "7月收益": "monthly_income_report",
            "当前期权风险主要集中在哪里": "option_positions_read",
            "分析最近的期权操作有没有不合理": "option_positions_read",
            "为什么 NVDA 没进候选": "candidate_filter_explain",
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
    assert calls[-2:] == [("为什么 NVDA 没进候选", "candidate"), ("结论呢", "review")]
    assert payload["cases"][-1]["tool_names"] == []


def test_p1_eval_treats_host_observation_continuation_as_read_only(monkeypatch, tmp_path) -> None:
    def run_channel_request(**kwargs):
        tool = {
            "7月收益": "monthly_income_report",
            "当前期权风险主要集中在哪里": "option_positions_read",
            "分析最近的期权操作有没有不合理": "option_positions_read",
            "为什么 NVDA 没进候选": "candidate_filter_explain",
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
            "7月收益": "monthly_income_report",
            "当前期权风险主要集中在哪里": "option_positions_read",
            "分析最近的期权操作有没有不合理": "option_positions_read",
            "为什么 NVDA 没进候选": "candidate_filter_explain",
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
