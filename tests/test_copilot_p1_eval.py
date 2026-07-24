from __future__ import annotations

import os
import sys

from src.application.copilot.contracts import AppEvent, AppResult
from scripts import copilot_p1_eval


_MTD_QUESTION = "7月 mtd 的期权收益"
_MTD_CORRECTION = "我写的是mtd"
_MTD_RESPONSE = "结论：MTD 账户 lx+sy 的已实现 PnL、现金流和指派股票均已列明。"
_BIAS_QUESTION = "收益率很高，所以现在就应该加仓吗？请结合当前持仓风险判断"


def _scene_event() -> AppEvent:
    return AppEvent(
        "evt_scene",
        "run_1",
        "scene_prepared",
        "2026-07-11T00:00:00+00:00",
        {
            "scene": "om_chat",
            "scene_version": "v3",
            "fragments": [
                {"path": "prompts/base_behavior.md", "sha256": "a" * 64, "chars": 100},
                {"path": "prompts/soul.md", "sha256": "b" * 64, "chars": 100},
            ],
            "compiled_prompt_sha256": "c" * 64,
            "selected_toolsets": ["analysis"],
            "tool_count": 1,
            "tool_schema_sha256": "d" * 64,
        },
    )


def _expected_tool(question: str) -> str | None:
    return {
        _MTD_QUESTION: "option_performance_report",
        "当前期权风险主要集中在哪里": "option_positions_read",
        "分析6月的期权操作有没有不合理，需要优化的地方": "option_positions_read",
        "为什么 NVDA 没进候选": "candidate_filter_explain",
        "最近 close advice 为什么没有通知": "close_advice_read",
        _BIAS_QUESTION: "option_positions_read",
    }.get(question)


def _response(question: str) -> str:
    case = next(item for item in copilot_p1_eval.CASES if item.question == question)
    if question in {_MTD_QUESTION, _MTD_CORRECTION}:
        return _MTD_RESPONSE
    if case.output_mode == "raw_json":
        return '{"status":"ok","language":"zh-CN"}'
    if case.output_mode == "json_fence":
        return '```json\n{"status":"ok"}\n```'
    if case.output_mode == "markdown_fence":
        return "```markdown\n# 结论\n等待\n```"
    if case.name == "no_unsolicited_expansion":
        return "结论：可以，等待是合法的量化结论。"
    if case.name == "no_trade_is_valid":
        return "结论：不应该立刻交易。"
    if case.name == "quantitative_bias_challenge":
        return "结论：不能仅因收益率高就加仓；应先核对集中度和尾部风险。"
    return "结论：测试回答"


def _tool_input(question: str, tool: str | None) -> dict[str, str]:
    if question in {_MTD_QUESTION, _MTD_CORRECTION} and tool == "option_performance_report":
        return {"period": "mtd"}
    return {}


def test_p1_eval_recognizes_equivalent_evidence_limit_wording() -> None:
    assert copilot_p1_eval._mentions_evidence_limit("净收益不可可靠给出，因为证据不完整。")


def test_p1_eval_main_sets_explicit_runtime_root(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    observed: dict[str, str | None] = {}

    def run_eval(**kwargs):
        observed["runtime_root"] = os.environ.get("OM_RUNTIME_ROOT")
        return {
            "structural_pass": True,
            "evidence_pass": True,
            "answer_quality_pass": None,
        }

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


def test_p1_eval_main_fails_when_evidence_gate_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        copilot_p1_eval,
        "run_eval",
        lambda **_kwargs: {
            "structural_pass": True,
            "evidence_pass": False,
            "answer_quality_pass": None,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "copilot_p1_eval.py",
            "--assistant-config",
            "config.assistant.json",
        ],
    )

    assert copilot_p1_eval.main() == 1


def test_p1_eval_runs_fixed_questions_with_follow_up_context(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    def run_channel_request(**kwargs):
        question = kwargs["user_message"]
        calls.append((question, kwargs["conversation_id"]))
        tool = _expected_tool(question)
        events = [_scene_event(), *([] if tool is None else [
            AppEvent(
                "evt_1",
                "run_1",
                "tool_call",
                "2026-07-11T00:00:00+00:00",
                {"tool_name": tool, "tool_input": _tool_input(question, tool)},
            )
        ])]
        return AppResult(status="answered", user_response=_response(question), events=events)

    monkeypatch.setattr(copilot_p1_eval, "run_channel_request", run_channel_request)
    payload = copilot_p1_eval.run_eval(
        assistant_config="config.yaml",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    assert payload["structural_pass"] is True
    assert calls[:2] == [(_MTD_QUESTION, "income"), (_MTD_CORRECTION, "income")]
    assert payload["cases"][1]["tool_names"] == []
    assert ("分析6月的期权操作有没有不合理，需要优化的地方", "review") in calls
    assert ("只看lx账户，结论是什么", "review") in calls
    assert calls[-1] == ("结论呢", "review")
    assert payload["cases"][-1]["tool_names"] == []
    assert payload["scene_provenance_consistent"] is True
    assert payload["scene_fingerprints"] == [
        {
            "scene_version": "v3",
            "compiled_prompt_sha256": "c" * 64,
            "tool_schema_sha256": "d" * 64,
        }
    ]
    for name in ("raw_json_output", "json_fence_output", "markdown_source_output"):
        case = next(item for item in payload["cases"] if item["name"] == name)
        assert case["checks"]["output_contract_valid"] is True
        assert case["checks"]["scene_provenance_present"] is True


def test_p1_eval_treats_host_observation_continuation_as_read_only(monkeypatch, tmp_path) -> None:
    def run_channel_request(**kwargs):
        question = kwargs["user_message"]
        tool = _expected_tool(question)
        tools = [tool, "__read_observation__"] if tool else []
        events = [
            _scene_event(),
            *[
            AppEvent(
                f"evt_{index}",
                "run_1",
                "tool_call",
                "2026-07-11T00:00:00+00:00",
                {"tool_name": name, "tool_input": _tool_input(question, name)},
            )
            for index, name in enumerate(tools, start=1)
            ],
        ]
        return AppResult(status="answered", user_response=_response(question), events=events)

    monkeypatch.setattr(copilot_p1_eval, "run_channel_request", run_channel_request)
    payload = copilot_p1_eval.run_eval(
        assistant_config="config.yaml",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    assert payload["structural_pass"] is True


def test_p1_eval_rejects_generic_analysis_for_primary_mtd_performance(monkeypatch, tmp_path) -> None:
    def run_channel_request(**kwargs):
        question = kwargs["user_message"]
        needs_read = next(
            case.requires_read_observation
            for case in copilot_p1_eval.CASES
            if case.question == question
        )
        events = [_scene_event(), *([] if not needs_read else [
            AppEvent(
                "evt_1",
                "run_1",
                "tool_call",
                "2026-07-11T00:00:00+00:00",
                {"tool_name": "analysis_query", "tool_input": {"config_key": "us"}},
            )
        ])]
        return AppResult(status="answered", user_response=_response(question), events=events)

    monkeypatch.setattr(copilot_p1_eval, "run_channel_request", run_channel_request)
    payload = copilot_p1_eval.run_eval(
        assistant_config="config.yaml",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    operation_review = next(item for item in payload["cases"] if item["name"] == "operation_review")
    mtd_income = next(item for item in payload["cases"] if item["name"] == "july_mtd_option_income")
    assert operation_review["tool_names"] == ["analysis_query"]
    assert operation_review["checks"]["read_observation_used"] is True
    assert operation_review["checks"]["required_primary_tool_used"] is True
    assert mtd_income["checks"]["required_primary_tool_used"] is False
    assert payload["structural_pass"] is False


def test_p1_eval_rejects_wrong_mtd_period_and_narrowed_account(monkeypatch, tmp_path) -> None:
    def run_channel_request(**kwargs):
        question = kwargs["user_message"]
        if question in {_MTD_QUESTION, _MTD_CORRECTION}:
            return AppResult(
                status="answered",
                user_response="结论：MTD lx账户的已实现 PnL、现金流和指派股票均已列明。",
                events=[
                    AppEvent(
                        "evt_wrong_scope",
                        "run_1",
                        "tool_call",
                        "2026-07-11T00:00:00+00:00",
                        {
                            "tool_name": "option_performance_report",
                            "tool_input": {
                                "period": "month",
                                "month": "2026-07",
                                "account": "lx",
                            },
                        },
                    )
                ],
            )
        return AppResult(status="answered", user_response="结论：测试回答")

    monkeypatch.setattr(copilot_p1_eval, "run_channel_request", run_channel_request)
    payload = copilot_p1_eval.run_eval(
        assistant_config="config.yaml",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    mtd_income = next(item for item in payload["cases"] if item["name"] == "july_mtd_option_income")
    assert mtd_income["checks"]["required_primary_tool_used"] is True
    assert mtd_income["checks"]["required_primary_tool_input"] is False
    assert mtd_income["checks"]["required_response_terms_present"] is True
    assert payload["structural_pass"] is False


def test_p1_eval_records_one_case_failure_and_continues(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    def run_channel_request(**kwargs):
        question = kwargs["user_message"]
        calls.append(question)
        if question == _MTD_QUESTION:
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


def test_p1_eval_requires_scene_provenance(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        copilot_p1_eval,
        "run_channel_request",
        lambda **kwargs: AppResult(
            status="answered",
            user_response=_response(kwargs["user_message"]),
        ),
    )

    payload = copilot_p1_eval.run_eval(
        assistant_config="config.yaml",
        config_key="us",
        host_db=str(tmp_path / "host.sqlite3"),
    )

    assert payload["scene_provenance_consistent"] is False
    assert payload["structural_pass"] is False
    assert all(
        case["checks"]["scene_provenance_present"] is False
        for case in payload["cases"]
    )


def test_p1_eval_checks_scope_conclusion_and_protocol(monkeypatch, tmp_path) -> None:
    def run_channel_request(**kwargs):
        question = kwargs["user_message"]
        tool = _expected_tool(question)
        events = [] if tool is None else [
            AppEvent(
                "evt_1",
                "run_1",
                "tool_call",
                "2026-07-11T00:00:00+00:00",
                {"tool_name": tool, "tool_input": {**_tool_input(question, tool), "config_key": "hk"}},
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
        tool = _expected_tool(question)
        if question == "把 NVDA put 加进开仓记录":
            return AppResult(
                status="control_requested",
                control_request={"intent_name": "manual_trade_open", "arguments": {"symbol": "NVDA"}},
                events=[_scene_event()],
            )
        events = [_scene_event(), *([] if tool is None else [
            AppEvent(
                "evt_1",
                "run_1",
                "tool_call",
                "2026-07-11T00:00:00+00:00",
                {"tool_name": tool, "tool_input": _tool_input(question, tool)},
            )
        ])]
        return AppResult(status="answered", user_response=_response(question), events=events)

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
            _scene_event(),
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

    assert payload["schema_version"] == "om.copilot.p1_eval.v4"
    assert payload["runtime_version"]
    assert payload["model"]["provider"] == "openai"
    assert payload["model"]["model"] == "gpt-test"
    assert payload["answer_quality_pass"] is None
    assert payload["cases"][0]["tool_metrics"]["tool_call_count"] == 1
    assert payload["cases"][0]["evidence_pass"] is True
    assert payload["cases"][0]["scene_provenance"]["scene_version"] == "v3"


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
        "schema_version": "om.copilot.p1_eval.v4",
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
