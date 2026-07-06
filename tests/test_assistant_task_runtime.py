from __future__ import annotations

from datetime import date

import pytest

from src.application.assistant.answer_verifier import verify_response_shape
from src.application.assistant.coverage_verifier import verify_coverage
from src.application.assistant.copilot import compose_answer, derive_task_frame, plan_evidence
from src.application.assistant.evidence import build_evidence_bundle
from src.application.assistant.task_completion import check_task_completion
from src.application.assistant.task_contract import build_task_contract


def test_option_operation_review_task_preserves_two_month_scope() -> None:
    task = derive_task_frame(
        question="分析5月6月的期权操作有没有不合理，需要优化的地方",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )

    assert task.task_name == "option_operation_review"
    assert task.domain == "strategy"
    assert task.task_mode == "analyze"
    assert task.requested_effect == "read"
    assert task.scope.requested_months == ("2026-05", "2026-06")
    assert "option_operation_review" in [profile.name for profile in task.profiles]
    assert "overall_judgement" in task.required_answer
    assert "operation_patterns" in task.required_answer
    assert "optimization_options" in task.required_answer
    assert "evidence_boundary" in task.answer_shape


def test_monthly_income_analysis_profile_is_distinct_from_operation_review() -> None:
    task = derive_task_frame(
        question="6月收益主要来自哪里",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )

    assert task.task_name == "monthly_income_analysis"
    assert task.domain == "income"
    assert task.scope.requested_months == ("2026-06",)
    assert tuple(profile.name for profile in task.profiles) == ("monthly_income_analysis",)
    assert "drivers" in task.answer_shape


def test_net_cashflow_question_selects_income_profile_and_account_scope() -> None:
    task = derive_task_frame(
        question="分析 lx 6月的净现金流明细",
        request_context={},
        today=date(2026, 7, 6),
        conversation_context=None,
    )

    plan = plan_evidence(task)

    assert task.task_name == "monthly_income_analysis"
    assert task.scope.requested_months == ("2026-06",)
    assert task.scope.requested_accounts == ("lx",)
    assert plan.calls[0].tool_name == "analysis_query"
    assert plan.calls[0].arguments["account"] == "lx"
    assert plan.calls[0].arguments["month"] == "2026-06"
    assert "monthly_income_cashflow_rows" in plan.required_views


def test_monthly_income_shape_accepts_data_source_wording() -> None:
    question = "6月收益分析"
    task = derive_task_frame(
        question=question,
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )
    contract = build_task_contract(
        question=question,
        plan={"goal": question, "steps": [], "task_contract": task.task_contract_payload()},
        request_context={},
        today=date(2026, 7, 5),
    ).public_payload()

    result = verify_response_shape(
        "6月收益总结：lx 净收益 CNY 123.45，主要来自当前查询到的月度汇总结果；数据源是 OM 只读收益数据。",
        task_contract=contract,
        coverage={"status": "complete", "missing": [], "gaps": []},
    ).public_payload()

    assert "source_and_policy" not in result["missing_answer"]


def test_assigned_stock_income_wording_does_not_select_monthly_income_profile() -> None:
    task = derive_task_frame(
        question="被指派股票的收益",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )

    assert task.domain == "position"
    assert "monthly_income_analysis" not in [profile.name for profile in task.profiles]
    assert "main_drivers" not in task.required_answer
    assert "shares_remaining" in task.required_answer


def test_runtime_health_diagnosis_profile_for_health_check() -> None:
    task = derive_task_frame(
        question="系统健康检查",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )

    assert task.task_name == "runtime_health_diagnosis"
    assert task.requested_effect == "read"
    assert "runtime_health_diagnosis" in [profile.name for profile in task.profiles]


def test_option_operation_review_evidence_plan_builds_executable_calls() -> None:
    task = derive_task_frame(
        question="分析6月的期权操作有没有不合理，需要优化的地方",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )

    plan = plan_evidence(task)

    assert plan.task_name == "option_operation_review"
    assert [call.tool_name for call in plan.calls] == ["analysis_query"]
    assert plan.required_views == (
        "account_monthly_performance",
        "account_monthly_income_components",
        "monthly_income_cashflow_rows",
        "trade_events",
        "open_option_exposure",
        "strategy_config_by_symbol_account",
        "strategy_replay_read_surface",
    )
    assert plan.calls[0].arguments == {
        "views": [
            "account_monthly_performance",
            "account_monthly_income_components",
            "monthly_income_cashflow_rows",
            "trade_events",
            "open_option_exposure",
            "strategy_config_by_symbol_account",
            "strategy_replay_read_surface",
        ],
        "month": "2026-06",
        "limit": 200,
    }


def test_copilot_explicit_month_overrides_stale_context_month() -> None:
    task = derive_task_frame(
        question="分析6月的期权操作有没有不合理，需要优化的地方",
        request_context={},
        today=date(2026, 7, 6),
        conversation_context={
            "context_projection": {
                "available_evidence_refs": [{"safe_slots": {"month": ["2026-07"]}}],
                "recent_turns": [{"safe_slots": {"month": ["2026-07"]}}],
            }
        },
    )

    plan = plan_evidence(task)

    assert task.task_name == "option_operation_review"
    assert task.scope.requested_months == ("2026-06",)
    assert task.scope.context_mode == "none"
    assert plan.calls[0].arguments["month"] == "2026-06"
    assert "months" not in plan.calls[0].arguments


def test_copilot_fill_notice_plans_concrete_manual_trade_open_preview() -> None:
    task = derive_task_frame(
        question="sy 成交提醒: 【成交提醒】成功卖出2张$腾讯 260605 440.00 沽$，成交价格：0.86，此笔订单委托已全部成交",
        request_context={},
        today=date(2026, 7, 6),
        conversation_context=None,
    )

    plan = plan_evidence(task)

    assert len(plan.calls) == 1
    assert plan.calls[0].tool_name == "manual_trade_open"
    assert plan.calls[0].arguments == {"account": "sy"}


def test_copilot_fill_notice_plans_concrete_manual_trade_close_preview() -> None:
    task = derive_task_frame(
        question="sy 成交提醒: 【成交提醒】成功买入1张$腾讯 260629 450.00 沽$，成交价格：1.20，此笔订单委托已全部成交",
        request_context={},
        today=date(2026, 7, 6),
        conversation_context=None,
    )

    plan = plan_evidence(task)

    assert len(plan.calls) == 1
    assert plan.calls[0].tool_name == "manual_trade_close"
    assert plan.calls[0].arguments == {"account": "sy"}


def test_copilot_does_not_judge_option_review_when_analysis_views_have_no_rows() -> None:
    task = derive_task_frame(
        question="分析6月的期权操作有没有不合理，需要优化的地方",
        request_context={},
        today=date(2026, 7, 6),
        conversation_context=None,
    )

    answer, trace = compose_answer(
        task=task,
        tool_results=(
            {
                "ok": True,
                "data": {
                    "schema_version": "analysis.query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "rows": [],
                    "views_used": ["trade_events", "open_option_exposure"],
                    "view_datasets": {
                        "trade_events": {"rows": [], "row_count": 0},
                        "open_option_exposure": {"rows": [], "row_count": 0},
                    },
                    "evidence": {
                        "diagnostics": [
                            {
                                "answer_boundary": "cannot infer absence of problem from empty diagnostic result",
                            }
                        ]
                    },
                },
            },
        ),
    )

    assert trace["route"] == "copilot_no_matching_analysis_evidence"
    assert "不能判断期权操作是否不合理" in answer
    assert "行级记录为 0" in answer
    assert "空结果不能证明没有问题" in answer
    assert "cannot infer" not in answer
    assert "偏保守" not in answer
    assert "未发现单一异常模式" not in answer


def test_option_operation_review_partial_rows_are_not_complete() -> None:
    task = derive_task_frame(
        question="分析6月的期权操作有没有不合理，需要优化的地方",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )

    completion = check_task_completion(
        task=task,
        covered_views={"account_monthly_performance"},
        successful_tool_count=1,
    )

    assert completion.status == "need_more_evidence"
    assert "trade_events" in completion.missing_views
    assert completion.next_action == "followup_tool"


def test_option_operation_review_coverage_requires_detail_views() -> None:
    question = "分析6月的期权操作有没有不合理，需要优化的地方"
    task = derive_task_frame(
        question=question,
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )
    contract = build_task_contract(
        question=question,
        plan={"goal": question, "steps": [], "task_contract": task.task_contract_payload()},
        request_context={},
        today=date(2026, 7, 5),
    )
    bundle = build_evidence_bundle(
        question=question,
        plan={"goal": question},
        observations=[
            {
                "tool_name": "analysis_query",
                "ok": True,
                "payload": {"limit": 20},
                "output_contract": {
                    "schema_version": "analysis.query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                },
                "data": {
                    "source_label": "OM read-only analysis workspace",
                    "columns": ["month", "account", "net_income_cny"],
                    "rows": [{"month": "2026-06", "account": "lx", "net_income_cny": 2414}],
                    "row_count": 1,
                    "views_used": ["account_monthly_performance"],
                    "evidence": {
                        "coverage": {
                            "views": ["account_monthly_performance"],
                            "months": ["2026-06"],
                            "accounts": ["lx"],
                            "symbols": [],
                        }
                    },
                },
            }
        ],
    )

    coverage = verify_coverage(task_contract=contract, evidence_bundle=bundle).public_payload()

    assert coverage["status"] == "recoverable_gap"
    assert coverage["next_action"] == "followup_tool"
    assert {"operation_patterns", "optimization_options"} <= set(coverage["missing"])
    gaps = {item["kind"]: item for item in coverage["gaps"]}
    assert gaps["task_profile_required_views_missing"]["suggested_tool"] == "analysis_query"
    assert "trade_events" in gaps["task_profile_required_views_missing"]["suggested_views"]
    assert "open_option_exposure" in gaps["task_profile_required_views_missing"]["suggested_views"]


def test_option_operation_review_answer_shape_requires_judgement_patterns_and_options() -> None:
    question = "分析6月的期权操作有没有不合理，需要优化的地方"
    task = derive_task_frame(
        question=question,
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )
    contract = build_task_contract(
        question=question,
        plan={"goal": question, "steps": [], "task_contract": task.task_contract_payload()},
        request_context={},
        today=date(2026, 7, 5),
    ).public_payload()

    weak = verify_response_shape(
        "6月期权交易看起来正常，数据来源是 OM read-only analysis workspace。",
        task_contract=contract,
        coverage={"status": "complete", "missing": [], "gaps": []},
    ).public_payload()
    strong = verify_response_shape(
        "结论：6月期权操作整体不算理想。问题模式：0700.HK 和 FUTU 的指派/现金占用偏集中，部分交易更像被动接货。优化建议：下月降低单一标的敞口，优先选择权利金覆盖更充分的合约，并把接货资金上限前置。证据边界：仅基于 OM read-only analysis workspace 已读取的交易、敞口和策略证据。",
        task_contract=contract,
        coverage={"status": "complete", "missing": [], "gaps": []},
    ).public_payload()

    assert {"operation_patterns", "optimization_options"} <= set(weak["missing_answer"])
    assert strong["violations"] == []


def test_conclusion_followup_inherits_prior_option_review_task_profile() -> None:
    task = derive_task_frame(
        question="结论呢",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context={
            "context_projection": {
                "recent_turns": [
                    {
                        "turn_id": "turn_option_review",
                        "user_summary": "分析6月的期权操作有没有不合理，需要优化的地方",
                        "assistant_summary": "Returned option operation rows",
                        "safe_slots": {"month": ["2026-06"]},
                        "evidence_refs": ["ev_option_review"],
                    }
                ],
                "available_evidence_refs": [
                    {
                        "ref_id": "ev_option_review",
                        "turn_id": "turn_option_review",
                        "source_tool": "analysis_query",
                        "safe_slots": {"month": ["2026-06"]},
                        "data_shape": {"views_used": ["account_monthly_performance", "monthly_income_cashflow_rows"]},
                    }
                ],
            }
        },
    )

    assert task.task_name == "option_operation_review"
    assert task.scope.requested_months == ("2026-06",)
    assert "operation_patterns" in task.required_answer


def test_conclusion_followup_inherits_latest_compatible_option_review_with_multiple_turns() -> None:
    task = derive_task_frame(
        question="结论呢",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context={
            "context_projection": {
                "recent_turns": [
                    {"turn_id": "turn_noise", "user_summary": "今天状态", "assistant_summary": "正常"},
                    {
                        "turn_id": "turn_review",
                        "user_summary": "分析6月的期权操作有没有不合理，需要优化的地方",
                        "assistant_summary": "读取了交易和敞口证据",
                        "safe_slots": {"month": ["2026-06"]},
                        "evidence_refs": ["ev_review"],
                    },
                ],
                "available_evidence_refs": [
                    {
                        "ref_id": "ev_review",
                        "turn_id": "turn_review",
                        "source_tool": "analysis_query",
                        "safe_slots": {"month": ["2026-06"]},
                        "data_shape": {"views_used": ["trade_events", "open_option_exposure"]},
                    }
                ],
            }
        },
    )

    assert task.task_name == "option_operation_review"
    assert task.scope.requested_months == ("2026-06",)
    assert "operation_patterns" in task.required_answer


def test_copilot_assigned_stock_followup_carries_account_and_symbol_scope() -> None:
    task = derive_task_frame(
        question="继续看这个指派正股浮盈",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context={
            "context_projection": {
                "recent_turns": [
                    {
                        "turn_id": "turn_assigned_stock",
                        "safe_slots": {
                            "account": ["sy"],
                            "symbol": ["0700.HK"],
                            "action": ["assigned-stock"],
                            "status": ["open"],
                        },
                    }
                ],
                "available_evidence_refs": [
                    {
                        "ref_id": "ev_assigned_stock",
                        "safe_slots": {
                            "account": ["sy"],
                            "symbol": ["0700.HK"],
                            "action": ["assigned-stock"],
                            "status": ["open"],
                        },
                    }
                ],
            }
        },
    )

    plan = plan_evidence(task)

    assert task.task_name == "assigned_stock_review"
    assert task.scope.requested_accounts == ("sy",)
    assert task.scope.requested_symbols == ("0700.HK",)
    assert task.scope.context_mode == "carry"
    assert plan.calls[0].tool_name == "option_positions_read"
    assert plan.calls[0].arguments == {
        "action": "assigned-stock",
        "status": "open",
        "refresh_quotes": True,
        "account": "sy",
        "symbol": "0700.HK",
    }


@pytest.mark.parametrize(
    ("question", "profile_name", "expected_view"),
    [
        ("分析6月的期权操作有没有不合理，需要优化的地方", "option_operation_review", "trade_events"),
        ("复盘5月6月期权交易哪里做得不好", "option_operation_review", "trade_events"),
        ("下个月期权操作怎么优化", "option_operation_review", "strategy_replay_read_surface"),
        ("6月收益主要来自哪里", "monthly_income_analysis", "account_monthly_income_components"),
        ("5月和6月哪个账户表现更好", "monthly_income_analysis", "account_monthly_performance"),
        ("账户净收入怎么算", "monthly_income_analysis", "account_monthly_income_components"),
        ("当前持仓有什么风险", "position_risk_diagnosis", "open_option_exposure"),
        ("哪些期权快到期", "position_risk_diagnosis", "expiration_risk_buckets"),
        ("当前敞口集中在哪些标的", "position_risk_diagnosis", "open_option_exposure"),
        ("为什么 PDD 没通过筛选", "candidate_strategy_diagnosis", "candidate_filter_diagnostics"),
        ("泡泡玛特参数是不是太严", "candidate_strategy_diagnosis", "strategy_config_by_symbol_account"),
        ("NVDA 过滤原因是什么", "candidate_strategy_diagnosis", "candidate_filter_diagnostics"),
        ("最近为什么没有 close advice", "close_advice_review", "close_advice_snapshot"),
        ("close advice 健康度怎么样", "close_advice_review", "close_advice_snapshot"),
        ("平仓建议为什么没出", "close_advice_review", "close_advice_snapshot"),
        ("今天有没有正常扫描", "runtime_health_diagnosis", "runtime_tick_status"),
        ("为什么没通知", "runtime_health_diagnosis", "runtime_tick_status"),
        ("线上运行状态怎么样", "runtime_health_diagnosis", "runtime_tick_status"),
    ],
)
def test_free_form_questions_select_task_profiles_and_evidence_views(
    question: str,
    profile_name: str,
    expected_view: str,
) -> None:
    task = derive_task_frame(
        question=question,
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )

    plan = plan_evidence(task)

    assert task.task_name == profile_name
    assert task.profiles[0].name == profile_name
    assert expected_view in plan.required_views
