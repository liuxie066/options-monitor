from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest


DESIGN_DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "AGENT_RELIABILITY_P0_P2_DESIGN.md"
AGENT_EVAL_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_agent_eval.jsonl"
TRACE_ROUTE_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_trace_route_samples.jsonl"
ASSISTANT_EVIDENCE_TEST_PATH = Path(__file__).parent / "test_assistant_evidence_session.py"
ASSISTANT_AGENT_EVAL_TEST_PATH = Path(__file__).parent / "test_assistant_agent_eval.py"
ASSISTANT_RUNTIME_TEST_PATH = Path(__file__).parent / "test_assistant_runtime.py"
AGENT_PLUGIN_CONTRACT_TEST_PATH = Path(__file__).parent / "test_agent_plugin_contract.py"
CONFIG_YAML_TEST_PATH = Path(__file__).parent / "test_config_yaml.py"

P2_RELEASE_GAP_GATE_COMMANDS = {
    "真实 session route 样本": "tests/test_assistant_evidence_session.py::test_format_assistant_trace_route_samples_from_fixture",
    "runtime stale / conflict": "tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py tests/test_assistant_runtime.py tests/test_analysis_tools.py",
    "upgrade conflict / command log missing / release status": "tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py tests/test_assistant_runtime.py tests/test_analysis_tools.py",
    "scope expansion 误判": "tests/test_assistant_agent_eval.py::test_assistant_agent_eval_fixture_covers_p2_agent_eval_gap_groups",
    "answer source/freshness": "tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py tests/test_assistant_runtime.py tests/test_analysis_tools.py",
    "发布 gate": "tests/test_release_test_plan.py::test_release_test_plan_covers_documented_p2_release_gaps",
}
P2_CLOSURE_COMPLETION_EVIDENCE = {
    "upgrade missing version / receipt 不再被 coverage 判为 complete。": {
        "test_coverage_marks_upgrade_missing_version_and_receipt_unrecoverable",
    },
    "不可补缺口不会触发 follow-up 工具循环。": {
        "test_agent_loop_does_not_replan_unrecoverable_upgrade_gap",
    },
    "可补缺口只会触发一次同 scope 只读查询。": {
        "test_agent_loop_replans_operation_timeline_for_recoverable_upgrade_gap",
        "test_agent_loop_replans_read_only_followup_for_recoverable_quote_gap",
    },
    "final answer 能自然说明缺口和影响，不展示内部 trace。": {
        "test_agent_loop_does_not_replan_unrecoverable_upgrade_gap",
        "test_assistant_agent_eval_uses_guarded_answer_evidence",
    },
    "golden eval 覆盖“缺版本/缺回执”和“证据完整”两类 upgrade status 问题。": {
        "test_assistant_agent_eval_fixture_covers_p2_agent_eval_gap_groups",
        "test_assistant_agent_eval_uses_guarded_answer_evidence",
    },
}
P2_RELEASE_GAP_EVIDENCE = {
    "真实 session route 样本": {
        "trace": {
            "trace_ask_missing_account",
            "trace_preview_manual_trade",
            "trace_rewrite_upgrade_conflict",
            "trace_fallback_bad_answer",
            "trace_denied_cross_account_write",
            "trace_pass_release_workflow_published",
            "trace_ask_read_scope_expansion",
            "trace_rewrite_runtime_notification_missing",
            "trace_rewrite_runtime_notification_conflict",
            "trace_rewrite_runtime_freshness_gap",
            "trace_rewrite_runtime_scheduler_skip",
            "trace_rewrite_quote_stale_freshness",
            "trace_rewrite_upgrade_stale_timeline",
            "trace_pass_operation_readback_applied",
            "trace_pass_operation_readback_cancelled",
            "trace_pass_upgrade_readback_cancelled",
            "trace_rewrite_release_no_matching_rows",
        },
    },
    "runtime stale / conflict": {
        "agent": {
            "runtime_why_conflict_stale_answer",
            "runtime_freshness_gap_answer",
            "runtime_notification_missing_not_success_answer",
            "runtime_notification_status_conflict_answer",
            "runtime_scheduler_skip_market_window_answer",
        },
        "trace": {
            "trace_rewrite_runtime_notification_missing",
            "trace_rewrite_runtime_notification_conflict",
            "trace_rewrite_runtime_freshness_gap",
            "trace_rewrite_runtime_scheduler_skip",
        },
    },
    "upgrade conflict / command log missing / release status": {
        "agent": {
            "operation_upgrade_conflict_command_log_missing_answer",
            "operation_upgrade_stale_timeline_answer",
            "operation_upgrade_release_tag_not_enough_answer",
            "operation_upgrade_release_published_answer",
            "operation_upgrade_release_failed_answer",
            "operation_upgrade_release_outcome_conflict_answer",
            "operation_upgrade_release_no_matching_rows_answer",
        },
        "trace": {
            "trace_rewrite_upgrade_conflict",
            "trace_rewrite_upgrade_stale_timeline",
            "trace_pass_release_workflow_published",
            "trace_rewrite_release_no_matching_rows",
        },
    },
    "scope expansion 误判": {
        "agent": {
            "action_safety_read_followup_same_scope_allowed",
            "action_safety_read_scope_expansion_asks",
            "action_safety_read_sql_account_scope_expansion_asks",
            "action_safety_read_sql_symbol_scope_expansion_asks",
            "action_safety_read_sql_period_scope_expansion_asks",
            "action_safety_cross_account_write_denied",
        },
        "trace": {
            "trace_ask_read_scope_expansion",
            "trace_ask_sql_period_scope_expansion",
            "trace_denied_cross_account_write",
        },
    },
    "answer source/freshness": {
        "agent": {
            "assigned_stock_missing_quote_answer",
            "analysis_quote_stale_answer_discloses_freshness",
            "runtime_freshness_gap_answer",
            "candidate_why_partial_confidence_answer",
            "candidate_missing_artifact_answer",
            "operation_upgrade_stale_timeline_answer",
        },
        "trace": {
            "trace_rewrite_quote_stale_freshness",
            "trace_rewrite_runtime_freshness_gap",
            "trace_rewrite_upgrade_stale_timeline",
        },
    },
    "发布 gate": {
        "tests": {
            "test_release_test_plan_maps_agent_reliability_changes_to_p2_gate",
            "test_release_test_plan_covers_documented_p2_release_gaps",
            "test_release_test_plan_covers_documented_p2_release_readiness",
        },
    },
}

P2_RELEASE_READINESS_GATE_COMMANDS = {
    "eval 覆盖第 6.19 的六类缺口，且没有只靠固定文案通过的测试。": (
        "tests/test_assistant_agent_eval.py::test_assistant_agent_eval_minimum_cases_satisfy_online_sample_contract"
    ),
    "`assistant_trace` 能解释最新一次线上失败的 route。": (
        "tests/test_assistant_evidence_session.py::test_format_assistant_trace_route_samples_from_fixture"
    ),
    "final answer 不展示 `analysis_query`、SQL、local path、internal id、lot id、raw command log、`canonical`、`synthesis`。": (
        "tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py tests/test_assistant_runtime.py tests/test_analysis_tools.py"
    ),
    "读请求不会生成 write preview；write 请求不会被 planner 直接 apply。": (
        "tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py tests/test_assistant_runtime.py tests/test_analysis_tools.py"
    ),
    "upgrade / runtime / quote / candidate why 的 missing、stale、conflict 都能降级回答。": (
        "tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py tests/test_assistant_runtime.py tests/test_analysis_tools.py"
    ),
}
P2_CODE_ACCEPTANCE_EVIDENCE = {
    "ActionSafety": {
        "model": {
            "test:test_action_safety_denies_prompt_injection_chain_to_write",
            "test:test_action_safety_detects_sql_only_account_scope_expansion",
        },
        "runtime": {
            "test:test_assistant_runtime_agent_loop_action_safety_rejects_preview_for_read_request",
            "test:test_assistant_runtime_rejects_llm_injected_write_preview_when_question_is_read_only",
        },
        "trace_eval": {
            "agent:prompt_injection_from_tool_output_denied",
            "agent:write_preview_no_apply_manual_trade_open",
            "trace:trace_preview_manual_trade",
            "trace:trace_denied_cross_account_write",
        },
    },
    "Quote diagnostics": {
        "model": {
            "test:test_evidence_bundle_extracts_contract_facts_and_missing_quote",
            "test:test_evidence_bundle_infers_quote_freshness_gap_with_as_of_from_rows",
            "test:test_answer_verifier_rejects_unsupported_quote_upstream_root_cause",
        },
        "runtime": {
            "test:test_assistant_runtime_renders_assigned_stock_missing_quote_explicitly",
            "test:test_agent_loop_replans_read_only_followup_for_recoverable_quote_gap",
            "test:test_assistant_runtime_agent_loop_assigned_stock_falls_back_from_invented_amount",
        },
        "trace_eval": {
            "agent:assigned_stock_missing_quote_answer",
            "agent:assigned_stock_fresh_quote_answer",
            "agent:analysis_quote_stale_answer_discloses_freshness",
            "trace:trace_rewrite_quote_stale_freshness",
        },
    },
    "Candidate why": {
        "model": {
            "test:test_analysis_query_candidate_filter_diagnostics_reads_trace_artifact",
            "test:test_evidence_bundle_infers_candidate_diagnostics_from_analysis_rows",
            "test:test_evidence_bundle_extracts_candidate_filter_explain_observed_trace",
            "test:test_evidence_bundle_marks_candidate_filter_explain_no_matching_trace_as_missing",
            "test:test_evidence_bundle_marks_analysis_diagnostic_missing_and_conflict",
        },
        "runtime": {
            "test:test_assistant_runtime_agent_loop_answer_guard_falls_back_on_missing_diagnostic_root_cause",
        },
        "trace_eval": {
            "agent:candidate_filter_explain_observed_rejection_answer",
            "agent:candidate_missing_artifact_answer",
            "agent:candidate_why_partial_confidence_answer",
        },
    },
    "Runtime why": {
        "model": {
            "test:test_evidence_bundle_infers_runtime_skip_diagnostics_from_analysis_rows",
            "test:test_evidence_bundle_infers_runtime_notification_status_conflict_from_rows",
            "test:test_answer_verifier_rejects_runtime_freshness_gap_root_cause",
        },
        "runtime": {
            "test:test_analysis_query_runtime_tick_status_uses_runtime_read_surface",
            "test:test_analysis_query_runtime_tick_status_surfaces_notification_conflict",
            "test:test_analysis_query_runtime_tick_status_surfaces_scheduler_reason",
        },
        "trace_eval": {
            "agent:runtime_why_conflict_stale_answer",
            "agent:runtime_notification_missing_not_success_answer",
            "agent:runtime_notification_status_conflict_answer",
            "agent:runtime_scheduler_skip_market_window_answer",
            "agent:runtime_freshness_gap_answer",
            "trace:trace_rewrite_runtime_notification_missing",
            "trace:trace_rewrite_runtime_notification_conflict",
            "trace:trace_rewrite_runtime_freshness_gap",
            "trace:trace_rewrite_runtime_scheduler_skip",
        },
    },
    "Upgrade receipt": {
        "model": {
            "test:test_evidence_bundle_extracts_upgrade_missing_version_diagnostics",
            "test:test_coverage_marks_upgrade_missing_version_and_receipt_unrecoverable",
            "test:test_analysis_query_upgrade_operation_status_reports_missing_command_log_artifact",
        },
        "runtime": {
            "test:test_agent_loop_replans_operation_timeline_for_recoverable_upgrade_gap",
            "test:test_agent_loop_does_not_replan_unrecoverable_upgrade_gap",
        },
        "trace_eval": {
            "agent:analysis_upgrade_receipt_missing_version",
            "agent:operation_upgrade_followup_timeline_completes_answer",
            "agent:operation_upgrade_conflict_command_log_missing_answer",
            "trace:trace_rewrite_upgrade_conflict",
        },
    },
    "Coverage / upgrade status": {
        "model": {
            "test:test_coverage_marks_upgrade_status_conflict_unrecoverable",
            "test:test_coverage_allows_operation_timeline_followup_before_timeline_is_queried",
            "test:test_coverage_accepts_complete_upgrade_status_evidence",
        },
        "runtime": {
            "test:test_agent_loop_replans_operation_timeline_for_recoverable_upgrade_gap",
            "test:test_agent_loop_does_not_replan_unrecoverable_upgrade_gap",
        },
        "trace_eval": {
            "agent:operation_upgrade_release_tag_not_enough_answer",
            "agent:operation_upgrade_release_published_answer",
            "agent:operation_upgrade_release_failed_answer",
            "agent:operation_upgrade_release_outcome_conflict_answer",
            "agent:operation_upgrade_release_no_matching_rows_answer",
            "trace:trace_rewrite_upgrade_stale_timeline",
            "trace:trace_pass_release_workflow_published",
            "trace:trace_rewrite_release_no_matching_rows",
        },
    },
    "Hook wrapper": {
        "model": {
            "test:test_tool_executor_precheck_rejects_planner_system_arguments",
            "test:test_tool_executor_allows_system_injected_scope_fields",
            "test:test_tool_executor_postcheck_marks_assigned_stock_missing_quote_warning",
        },
        "runtime": {
            "test:test_agent_loop_tool_result_contains_evidence_bundle_and_session",
            "test:test_assistant_runtime_agent_loop_plans_manual_trade_open_preview",
        },
        "trace_eval": {
            "test:test_format_assistant_trace_compact_redacts_internal_details",
            "trace:trace_preview_manual_trade",
            "trace:trace_pass_operation_readback_applied",
            "trace:trace_pass_operation_readback_cancelled",
        },
    },
}
P2_ROUTE_PRIORITY_EVIDENCE = {
    "deny": {
        "test:test_assistant_runtime_rejects_llm_injected_write_preview_when_question_is_read_only",
        "test:test_assistant_runtime_agent_loop_action_safety_rejects_preview_for_read_request",
        "trace:trace_denied_cross_account_write",
    },
    "ask": {
        "test:test_agent_loop_replans_analysis_query_for_missing_account_coverage",
        "trace:trace_ask_missing_account",
        "trace:trace_ask_read_scope_expansion",
    },
    "fallback": {
        "test:test_assistant_runtime_agent_loop_answer_guard_falls_back_on_missing_diagnostic_root_cause",
        "test:test_format_assistant_trace_compact_redacts_internal_details",
        "trace:trace_fallback_bad_answer",
    },
    "rewrite": {
        "test:test_assistant_runtime_agent_loop_answer_guard_rewrites_internal_ux_leak",
        "test:test_format_assistant_trace_shows_key_routes",
        "trace:trace_rewrite_upgrade_conflict",
        "trace:trace_rewrite_runtime_notification_missing",
        "trace:trace_rewrite_quote_stale_freshness",
    },
    "pass": {
        "test:test_agent_loop_tool_result_contains_evidence_bundle_and_session",
        "test:test_format_assistant_trace_route_samples_from_fixture",
        "trace:trace_pass_release_workflow_published",
        "trace:trace_pass_operation_readback_applied",
    },
}
P2_EVIDENCE_TRACE_OWNERSHIP_EVIDENCE = {
    "最终回答只从 `EvidenceBundle` 和 deterministic renderer 取事实。": {
        "test_assistant_agent_eval_uses_guarded_answer_evidence",
        "test_assistant_runtime_agent_loop_assigned_stock_falls_back_from_invented_amount",
        "test_assistant_runtime_agent_loop_answer_guard_rewrites_contradictory_income_synthesis",
    },
    "`hook_results` 只说明“为什么能答/不能答”，不能新增业务事实。": {
        "test_agent_loop_tool_result_contains_evidence_bundle_and_session",
        "test_format_assistant_trace_compact_redacts_internal_details",
    },
    "`assistant_trace` 展示的是 compact trace，不展示 raw observation。": {
        "test_agent_loop_tool_result_contains_evidence_bundle_and_session",
        "test_format_assistant_trace_compact_redacts_internal_details",
        "test_assistant_trace_route_samples_satisfy_online_sample_contract",
    },
    "session store 只能持久化必要摘要；敏感内部字段必须在写入 compact trace 前裁剪。": {
        "test_agent_loop_tool_result_contains_evidence_bundle_and_session",
        "test_assistant_runtime_agent_loop_plans_manual_trade_open_preview",
        "test_assistant_runtime_agent_loop_cancels_manual_trade_open_preview",
    },
    "测试要同时断言“答案有必要业务字段”和“答案没有内部字段”。": {
        "test_assistant_agent_eval_minimum_cases_satisfy_online_sample_contract",
        "test_assistant_agent_eval_uses_guarded_answer_evidence",
        "test_format_assistant_trace_compact_redacts_internal_details",
    },
}
P2_FAILURE_HANDLING_ROUTE_EVIDENCE = {
    "Task scope 不足": {
        "route": "`ask`",
        "evidence": {
            "test:test_format_assistant_trace_shows_key_routes",
            "trace:trace_ask_missing_account",
            "trace:trace_ask_read_scope_expansion",
        },
    },
    "Coverage gap 可补": {
        "route": "bounded follow-up",
        "evidence": {
            "test:test_agent_loop_replans_analysis_query_for_missing_account_coverage",
            "agent:analysis_income_compare_missing_sy_followup_answer",
            "agent:assigned_stock_missing_quote_answer",
        },
    },
    "Coverage gap 不可补": {
        "route": "`fallback` / `ask`",
        "evidence": {
            "test:test_agent_loop_does_not_replan_unrecoverable_upgrade_gap",
            "agent:operation_upgrade_conflict_command_log_missing_answer",
            "trace:trace_fallback_bad_answer",
        },
    },
    "ActionPolicy deny": {
        "route": "`deny`",
        "evidence": {
            "test:test_tool_executor_preserves_action_policy_denial",
            "test:test_assistant_runtime_agent_loop_rejects_disallowed_plan_tool",
        },
    },
    "ActionSafety deny": {
        "route": "`deny`",
        "evidence": {
            "test:test_action_safety_denies_preview_for_read_only_request",
            "test:test_assistant_runtime_agent_loop_action_safety_rejects_preview_for_read_request",
            "trace:trace_denied_cross_account_write",
        },
    },
    "Root cause unsupported": {
        "route": "`rewrite`，失败后 `fallback`",
        "evidence": {
            "test:test_answer_verifier_rejects_unsupported_quote_upstream_root_cause",
            "test:test_assistant_runtime_agent_loop_answer_guard_falls_back_on_unsupported_quote_root_cause",
            "agent:assigned_stock_missing_quote_answer",
            "trace:trace_rewrite_quote_stale_freshness",
        },
    },
    "Trace leak": {
        "route": "`rewrite`，失败后 `fallback`",
        "evidence": {
            "test:test_assistant_runtime_agent_loop_answer_guard_rewrites_internal_ux_leak",
            "test:test_format_assistant_trace_compact_redacts_internal_details",
            "trace:trace_fallback_bad_answer",
        },
    },
    "Hook conflict": {
        "route": "`fallback` / `ask`",
        "evidence": {
            "test:test_answer_verifier_rejects_definitive_status_on_conflicting_diagnostics",
            "agent:runtime_notification_status_conflict_answer",
            "agent:operation_upgrade_release_outcome_conflict_answer",
            "trace:trace_rewrite_runtime_notification_conflict",
        },
    },
}
P2_BOUNDED_FOLLOWUP_DENY_EVIDENCE = {
    "planner 想 apply/confirm/cancel。": {
        "test:test_action_policy_allows_planner_preview_without_apply_authority",
        "test:test_action_safety_allows_matching_preview_without_apply_authority",
        "test:test_assistant_runtime_agent_loop_rejects_confirm_plan",
        "test:test_assistant_runtime_agent_loop_rejects_disallowed_plan_tool",
        "trace:trace_denied_planner_apply",
    },
    "缺口来自写入确认。": {
        "test:test_assistant_runtime_agent_loop_cancels_manual_trade_open_preview",
        "test:test_assistant_runtime_agent_loop_plans_manual_trade_open_preview",
        "agent:write_preview_no_apply_manual_trade_open",
        "trace:trace_preview_manual_trade",
    },
    "缺口需要启动服务、补发通知、broker-facing 操作、启动/修复 OpenD，或发布远端 release。": {
        "test:test_agent_loop_does_not_replan_unrecoverable_upgrade_gap",
        "test:test_coverage_marks_release_publication_status_missing_unrecoverable",
        "test:test_coverage_marks_upgrade_missing_version_and_receipt_unrecoverable",
        "agent:operation_upgrade_release_tag_not_enough_answer",
        "agent:runtime_notification_missing_not_success_answer",
    },
    "follow-up 会扩大到用户未要求的账户、标的、月份。": {
        "test:test_agent_loop_rejects_unrelated_followup_plan_for_evidence_gap",
        "agent:action_safety_read_scope_expansion_asks",
        "agent:action_safety_read_sql_account_scope_expansion_asks",
        "agent:action_safety_read_sql_period_scope_expansion_asks",
        "agent:action_safety_read_sql_symbol_scope_expansion_asks",
        "trace:trace_ask_read_scope_expansion",
        "trace:trace_ask_sql_period_scope_expansion",
    },
}
P2_NOT_DO_BOUNDARY_EVIDENCE = {
    "不做 Permission Profile。": {
        "test_yaml_config_rejects_write_gates",
        "test_action_policy_denies_non_read_tool_without_adding_authority",
    },
    "不做用户可配置 hook。": {
        "test_yaml_assistant_config_rejects_user_configurable_hooks",
    },
    "不做第二套 tool registry、session store、planner 或权限系统。": {
        "test_agent_registry_manifest_and_tool_objects_stay_in_sync",
        "test_agent_loop_planner_catalog_matches_registry_backed_manifest",
        "test_message_less_local_agent_sessions_do_not_overwrite_each_other",
        "test_action_policy_denies_non_read_tool_without_adding_authority",
    },
    "不让 LLM 直接决定 `allow`、`deny`、`apply`、`confirm`。": {
        "test_assistant_capability_catalog_has_safe_llm_invariants",
        "test_action_policy_allows_planner_preview_without_apply_authority",
        "test_action_safety_allows_matching_preview_without_apply_authority",
    },
    "不把 tool output 中的指令当作下一步授权。": {
        "test_action_safety_denies_prompt_injection_chain_to_write",
        "test_assistant_runtime_rejects_llm_injected_write_preview_when_question_is_read_only",
    },
    "不在普通回答里展示 SQL、path、internal id、lot id、raw command log。": {
        "test_assistant_agent_eval_uses_guarded_answer_evidence",
        "test_assistant_runtime_agent_loop_answer_guard_rewrites_internal_ux_leak",
        "test_format_assistant_trace_compact_redacts_internal_details",
    },
    "不为了一个线上问题新增专用工具；优先增强通用 evidence、coverage、trace 和 eval。": {
        "test_agent_registry_manifest_and_tool_objects_stay_in_sync",
        "test_assistant_agent_eval_minimum_cases_satisfy_online_sample_contract",
        "test_release_test_plan_maps_agent_reliability_changes_to_p2_gate",
    },
    "`symbol_resolve` 属于通用 symbol identity read tool，不是 candidate-filter 专用": {
        "test_symbol_resolve_canonical_symbol_supports_symbol_claims",
        "test_task_contract_and_coverage_accept_candidate_filter_explain_trace_evidence",
        "test_evidence_bundle_infers_candidate_diagnostics_from_analysis_rows",
    },
}


def _normalize_doc_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _load_documented_p2_release_gap_names() -> set[str]:
    text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    start = text.index("### 6.19 P2 发布前缺口补齐")
    section = text[start:]
    end_match = re.search(r"\n不补：", section)
    if end_match:
        section = section[: end_match.start()]

    gaps: set[str] = set()
    for line in section.splitlines():
        match = re.match(r"\|\s*([^|]+?)\s*\|", line)
        if not match:
            continue
        gap = match.group(1).strip()
        if gap in {"缺口"} or set(gap) == {"-"}:
            continue
        gaps.add(gap)
    assert gaps
    return gaps


def _load_documented_p2_failure_handling_routes() -> dict[str, str]:
    text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    start = text.index("### 6.12 P2 失败处理策略")
    section = text[start:text.index("### 6.13 P2 代码验收矩阵", start)]

    routes: dict[str, str] = {}
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(parts) < 4:
            continue
        failure_point, route = parts[0], parts[2]
        if failure_point == "失败点" or set(failure_point) == {"-"}:
            continue
        routes[failure_point] = _normalize_doc_text(route)
    assert routes
    return routes


def _load_documented_p2_bounded_followup_denials() -> set[str]:
    text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    start = text.index("Bounded follow-up 只允许补同一用户问题所需的只读证据。以下情况不允许 follow-up：")
    section = text[start:text.index("### 6.13 P2 代码验收矩阵", start)]

    items: set[str] = set()
    for line in section.splitlines():
        match = re.match(r"\s*-\s+(.*)", line)
        if match:
            items.add(_normalize_doc_text(match.group(1)))
    assert items
    return items


def _load_documented_p2_closure_completion_items() -> set[str]:
    text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    start = text.index("### 6.18 P2 收口执行顺序")
    section = text[start:text.index("### 6.19 P2 发布前缺口补齐", start)]
    completion_start = section.index("完成判定：")
    section = section[completion_start:]

    items: list[str] = []
    current: list[str] = []
    for line in section.splitlines():
        match = re.match(r"\s*\d+\.\s+(.*)", line)
        if match:
            if current:
                items.append(_normalize_doc_text(" ".join(current)))
            current = [match.group(1)]
            continue
        if current and line.strip():
            current.append(line.strip())
    if current:
        items.append(_normalize_doc_text(" ".join(current)))
    assert items
    return set(items)


def _load_documented_p2_release_readiness_items() -> set[str]:
    text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    start = text.index("可发布判定：")
    section = text[start:]
    end = section.index("回退策略：")
    section = section[:end]

    items: list[str] = []
    current: list[str] = []
    for line in section.splitlines():
        match = re.match(r"\s*\d+\.\s+(.*)", line)
        if match:
            if current:
                items.append(_normalize_doc_text(" ".join(current)))
            current = [match.group(1)]
            continue
        if current and line.strip():
            current.append(line.strip())
    if current:
        items.append(_normalize_doc_text(" ".join(current)))
    assert items
    return set(items)


def _load_documented_p2_not_do_items() -> set[str]:
    text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    start = text.index("### 6.17 P2 不做清单")
    section = text[start:text.index("### 6.18 P2 收口执行顺序", start)]
    section = section[: section.index("如果后续确实需要新增工具")]

    items: set[str] = set()
    for line in section.splitlines():
        match = re.match(r"\s*-\s+(.*)", line)
        if match:
            items.add(_normalize_doc_text(match.group(1)))
    assert items
    return items


def _load_documented_p2_code_acceptance_slices() -> set[str]:
    text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    start = text.index("### 6.13 P2 代码验收矩阵")
    section = text[start:text.index("推荐执行顺序：", start)]

    slices: set[str] = set()
    for line in section.splitlines():
        match = re.match(r"\|\s*([^|]+?)\s*\|", line)
        if not match:
            continue
        name = match.group(1).strip()
        if name in {"Slice"} or set(name) == {"-"}:
            continue
        slices.add(name)
    assert slices
    return slices


def _load_documented_p2_route_priority() -> list[str]:
    text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    start = text.index("### 6.14 P2 Route Authority 补充")
    section = text[start:text.index("### 6.15 P2 Evidence And Trace Ownership", start)]
    match = re.search(r"deny\s*>\s*ask\s*>\s*fallback\s*>\s*rewrite\s*>\s*pass", section)
    assert match
    return [item.strip() for item in match.group(0).split(">")]


def _load_documented_p2_evidence_trace_ownership_constraints() -> set[str]:
    text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    start = text.index("### 6.15 P2 Evidence And Trace Ownership")
    section = text[start:text.index("### 6.16 P2 Current Implementation Audit", start)]
    section = section[section.index("实现约束：") :]

    items: list[str] = []
    current: list[str] = []
    for line in section.splitlines():
        match = re.match(r"\s*\d+\.\s+(.*)", line)
        if match:
            if current:
                items.append(_normalize_doc_text(" ".join(current)))
            current = [match.group(1)]
            continue
        if current and line.strip():
            current.append(line.strip())
    if current:
        items.append(_normalize_doc_text(" ".join(current)))
    assert items
    return set(items)


def _load_jsonl_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            ids.add(str(json.loads(text).get("id") or ""))
    assert ids
    return ids


def _load_test_function_names(paths: tuple[Path, ...]) -> set[str]:
    names = {name for name in globals() if name.startswith("test_")}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        names.update(re.findall(r"^def (test_[A-Za-z0-9_]+)\(", text, flags=re.MULTILINE))
    assert names
    return names


def _missing_evidence_refs(refs: set[str], *, test_names: set[str], agent_ids: set[str], trace_ids: set[str]) -> list[str]:
    missing: list[str] = []
    for ref in sorted(refs):
        kind, _, name = ref.partition(":")
        if kind == "test":
            if name not in test_names:
                missing.append(ref)
            continue
        if kind == "agent":
            if name not in agent_ids:
                missing.append(ref)
            continue
        if kind == "trace":
            if name not in trace_ids:
                missing.append(ref)
            continue
        missing.append(ref)
    return missing


def test_release_test_plan_maps_event_and_service_changes() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=[
            "src/application/events/source_futu.py",
            "src/application/service_upgrade.py",
        ],
        mode="standard",
        version="1.2.183",
    )

    assert plan["ok"] is True
    assert plan["risk"] == "standard"
    assert plan["requires_full_pytest"] is False
    assert plan["commands"][0] == "python3 scripts/release_check.py --tag v1.2.183"
    assert "git diff --check" in plan["commands"]
    assert "python3 -m pytest tests/test_event_prefetch.py tests/test_event_source_futu.py tests/test_event_risk_warn.py" in plan["commands"]
    assert (
        "python3 -m pytest tests/test_service_deploy.py tests/test_version_check.py tests/test_install_script.py "
        "tests/test_release_test_plan.py"
    ) in plan["commands"]
    assert "python3 scripts/generate_dependency_graph.py --check" in plan["commands"]
    assert {rule["name"] for rule in plan["matched_rules"]} >= {"event_source", "service_release"}


def test_release_test_plan_requires_full_pytest_for_ledger_changes() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=["domain/domain/ledger/projection.py"],
        mode="fast",
        version="v1.2.183",
    )

    assert plan["risk"] == "full"
    assert plan["requires_full_pytest"] is True
    assert plan["commands"][0] == "python3 scripts/release_check.py --tag v1.2.183"
    assert plan["commands"][-1] == "python3 -m pytest"


def test_release_test_plan_maps_agent_reliability_changes_to_p2_gate() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=[
            "src/application/assistant/coverage_verifier.py",
            "src/application/agent_tools/analysis.py",
            "tests/fixtures/assistant_agent_eval.jsonl",
        ],
        mode="standard",
        version="1.2.184",
    )

    assert plan["risk"] == "standard"
    assert plan["requires_full_pytest"] is False
    assert {rule["name"] for rule in plan["matched_rules"]} >= {"agent_reliability", "dependency_graph"}
    assert "jq -c . tests/fixtures/assistant_agent_eval.jsonl" in plan["commands"]
    assert "jq -c . tests/fixtures/assistant_trace_route_samples.jsonl" in plan["commands"]
    assert (
        "python3 -m pytest tests/test_assistant_agent_eval.py::test_assistant_agent_eval_fixture_covers_p2_agent_eval_gap_groups "
        "tests/test_assistant_agent_eval.py::test_assistant_agent_eval_fixture_covers_documented_p2_minimum_cases "
        "tests/test_assistant_agent_eval.py::test_assistant_agent_eval_minimum_case_mapping_matches_design_document "
        "tests/test_assistant_agent_eval.py::test_assistant_agent_eval_minimum_cases_satisfy_online_sample_contract"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_assistant_evidence_session.py::test_format_assistant_trace_route_samples_from_fixture "
        "tests/test_assistant_evidence_session.py::test_assistant_trace_fixture_covers_documented_p2_minimum_cases "
        "tests/test_assistant_evidence_session.py::test_assistant_trace_minimum_case_mapping_matches_design_document "
        "tests/test_assistant_evidence_session.py::test_assistant_trace_route_samples_satisfy_online_sample_contract"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_release_test_plan.py::test_release_test_plan_covers_documented_p2_release_gaps"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_release_test_plan.py::test_release_test_plan_covers_documented_p2_release_readiness"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_release_test_plan.py::test_documented_p2_failure_handling_routes_have_evidence"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_release_test_plan.py::test_documented_p2_bounded_followup_denials_have_evidence"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_release_test_plan.py::test_documented_p2_release_gaps_have_fixture_or_gate_evidence"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_release_test_plan.py::test_documented_p2_closure_completion_items_have_test_evidence"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_release_test_plan.py::test_documented_p2_not_do_items_have_boundary_evidence"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_release_test_plan.py::test_documented_p2_code_acceptance_matrix_has_three_layer_evidence"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_release_test_plan.py::test_documented_p2_route_priority_has_trace_or_eval_evidence"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_release_test_plan.py::test_documented_p2_evidence_trace_ownership_has_test_evidence"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py "
        "tests/test_assistant_runtime.py tests/test_analysis_tools.py"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py "
        "tests/test_candidate_filter_trace.py"
    ) in plan["commands"]


def test_release_test_plan_covers_documented_p2_release_gaps() -> None:
    from src.application.release_test_plan import build_release_test_plan

    documented_gaps = _load_documented_p2_release_gap_names()
    assert set(P2_RELEASE_GAP_GATE_COMMANDS) == documented_gaps

    plan = build_release_test_plan(
        changed_files=["docs/AGENT_RELIABILITY_P0_P2_DESIGN.md"],
        mode="standard",
        version="1.2.184",
    )
    commands = tuple(str(command) for command in plan["commands"])
    missing: dict[str, str] = {}
    for gap, command_part in P2_RELEASE_GAP_GATE_COMMANDS.items():
        if not any(command_part in command for command in commands):
            missing[gap] = command_part
    assert missing == {}


def test_release_test_plan_covers_documented_p2_release_readiness() -> None:
    from src.application.release_test_plan import build_release_test_plan

    documented_items = _load_documented_p2_release_readiness_items()
    assert set(P2_RELEASE_READINESS_GATE_COMMANDS) == documented_items

    plan = build_release_test_plan(
        changed_files=["docs/AGENT_RELIABILITY_P0_P2_DESIGN.md"],
        mode="standard",
        version="1.2.184",
    )
    commands = tuple(str(command) for command in plan["commands"])
    missing: dict[str, str] = {}
    for item, command_part in P2_RELEASE_READINESS_GATE_COMMANDS.items():
        if not any(command_part in command for command in commands):
            missing[item] = command_part
    assert missing == {}


def test_documented_p2_release_gaps_have_fixture_or_gate_evidence() -> None:
    documented_gaps = _load_documented_p2_release_gap_names()
    assert set(P2_RELEASE_GAP_EVIDENCE) == documented_gaps

    agent_ids = _load_jsonl_ids(AGENT_EVAL_FIXTURE_PATH)
    trace_ids = _load_jsonl_ids(TRACE_ROUTE_FIXTURE_PATH)
    test_names = {name for name in globals() if name.startswith("test_")}

    missing: dict[str, list[str]] = {}
    for gap, evidence in P2_RELEASE_GAP_EVIDENCE.items():
        absent: list[str] = []
        for case_id in sorted(evidence.get("agent") or ()):
            if case_id not in agent_ids:
                absent.append(f"agent:{case_id}")
        for case_id in sorted(evidence.get("trace") or ()):
            if case_id not in trace_ids:
                absent.append(f"trace:{case_id}")
        for test_name in sorted(evidence.get("tests") or ()):
            if test_name not in test_names:
                absent.append(f"test:{test_name}")
        if absent:
            missing[gap] = absent

    assert missing == {}


def test_documented_p2_failure_handling_routes_have_evidence() -> None:
    documented_routes = _load_documented_p2_failure_handling_routes()
    expected_routes = {key: str(value["route"]) for key, value in P2_FAILURE_HANDLING_ROUTE_EVIDENCE.items()}
    assert documented_routes == expected_routes

    test_names = _load_test_function_names(
        (
            ASSISTANT_RUNTIME_TEST_PATH,
            ASSISTANT_EVIDENCE_TEST_PATH,
            ASSISTANT_AGENT_EVAL_TEST_PATH,
        )
    )
    agent_ids = _load_jsonl_ids(AGENT_EVAL_FIXTURE_PATH)
    trace_ids = _load_jsonl_ids(TRACE_ROUTE_FIXTURE_PATH)

    missing: dict[str, list[str]] = {}
    for failure_point, route_evidence in P2_FAILURE_HANDLING_ROUTE_EVIDENCE.items():
        absent = _missing_evidence_refs(
            set(route_evidence["evidence"]),
            test_names=test_names,
            agent_ids=agent_ids,
            trace_ids=trace_ids,
        )
        if absent:
            missing[failure_point] = absent

    assert missing == {}


def test_documented_p2_bounded_followup_denials_have_evidence() -> None:
    documented_denials = _load_documented_p2_bounded_followup_denials()
    assert set(P2_BOUNDED_FOLLOWUP_DENY_EVIDENCE) == documented_denials

    test_names = _load_test_function_names(
        (
            ASSISTANT_RUNTIME_TEST_PATH,
            ASSISTANT_EVIDENCE_TEST_PATH,
            ASSISTANT_AGENT_EVAL_TEST_PATH,
        )
    )
    agent_ids = _load_jsonl_ids(AGENT_EVAL_FIXTURE_PATH)
    trace_ids = _load_jsonl_ids(TRACE_ROUTE_FIXTURE_PATH)

    missing: dict[str, list[str]] = {}
    for denial, refs in P2_BOUNDED_FOLLOWUP_DENY_EVIDENCE.items():
        absent = _missing_evidence_refs(
            refs,
            test_names=test_names,
            agent_ids=agent_ids,
            trace_ids=trace_ids,
        )
        if absent:
            missing[denial] = absent

    assert missing == {}


def test_documented_p2_closure_completion_items_have_test_evidence() -> None:
    documented_items = _load_documented_p2_closure_completion_items()
    assert set(P2_CLOSURE_COMPLETION_EVIDENCE) == documented_items

    test_names = _load_test_function_names(
        (
            ASSISTANT_EVIDENCE_TEST_PATH,
            ASSISTANT_AGENT_EVAL_TEST_PATH,
        )
    )
    missing: dict[str, list[str]] = {}
    for item, expected_tests in P2_CLOSURE_COMPLETION_EVIDENCE.items():
        absent = sorted(test_name for test_name in expected_tests if test_name not in test_names)
        if absent:
            missing[item] = absent

    assert missing == {}


def test_documented_p2_not_do_items_have_boundary_evidence() -> None:
    documented_items = _load_documented_p2_not_do_items()
    assert set(P2_NOT_DO_BOUNDARY_EVIDENCE) == documented_items

    test_names = _load_test_function_names(
        (
            ASSISTANT_RUNTIME_TEST_PATH,
            ASSISTANT_EVIDENCE_TEST_PATH,
            ASSISTANT_AGENT_EVAL_TEST_PATH,
            AGENT_PLUGIN_CONTRACT_TEST_PATH,
            CONFIG_YAML_TEST_PATH,
        )
    )
    missing: dict[str, list[str]] = {}
    for item, expected_tests in P2_NOT_DO_BOUNDARY_EVIDENCE.items():
        absent = sorted(test_name for test_name in expected_tests if test_name not in test_names)
        if absent:
            missing[item] = absent

    assert missing == {}


def test_documented_p2_code_acceptance_matrix_has_three_layer_evidence() -> None:
    documented_slices = _load_documented_p2_code_acceptance_slices()
    assert set(P2_CODE_ACCEPTANCE_EVIDENCE) == documented_slices

    test_names = _load_test_function_names(
        (
            ASSISTANT_RUNTIME_TEST_PATH,
            ASSISTANT_EVIDENCE_TEST_PATH,
            ASSISTANT_AGENT_EVAL_TEST_PATH,
            AGENT_PLUGIN_CONTRACT_TEST_PATH,
            CONFIG_YAML_TEST_PATH,
            Path(__file__).parent / "test_analysis_tools.py",
        )
    )
    agent_ids = _load_jsonl_ids(AGENT_EVAL_FIXTURE_PATH)
    trace_ids = _load_jsonl_ids(TRACE_ROUTE_FIXTURE_PATH)

    missing: dict[str, dict[str, list[str]]] = {}
    for slice_name, layers in P2_CODE_ACCEPTANCE_EVIDENCE.items():
        absent_layers: dict[str, list[str]] = {}
        for layer in ("model", "runtime", "trace_eval"):
            refs = layers.get(layer) or set()
            if not refs:
                absent_layers[layer] = ["<no evidence refs>"]
                continue
            absent = _missing_evidence_refs(
                refs,
                test_names=test_names,
                agent_ids=agent_ids,
                trace_ids=trace_ids,
            )
            if absent:
                absent_layers[layer] = absent
        if absent_layers:
            missing[slice_name] = absent_layers

    assert missing == {}


def test_documented_p2_route_priority_has_trace_or_eval_evidence() -> None:
    documented_priority = _load_documented_p2_route_priority()
    assert documented_priority == list(P2_ROUTE_PRIORITY_EVIDENCE)

    test_names = _load_test_function_names(
        (
            ASSISTANT_RUNTIME_TEST_PATH,
            ASSISTANT_EVIDENCE_TEST_PATH,
            ASSISTANT_AGENT_EVAL_TEST_PATH,
        )
    )
    agent_ids = _load_jsonl_ids(AGENT_EVAL_FIXTURE_PATH)
    trace_ids = _load_jsonl_ids(TRACE_ROUTE_FIXTURE_PATH)

    missing: dict[str, list[str]] = {}
    for route, refs in P2_ROUTE_PRIORITY_EVIDENCE.items():
        absent = _missing_evidence_refs(
            refs,
            test_names=test_names,
            agent_ids=agent_ids,
            trace_ids=trace_ids,
        )
        if absent:
            missing[route] = absent

    assert missing == {}


def test_documented_p2_evidence_trace_ownership_has_test_evidence() -> None:
    documented_items = _load_documented_p2_evidence_trace_ownership_constraints()
    assert set(P2_EVIDENCE_TRACE_OWNERSHIP_EVIDENCE) == documented_items

    test_names = _load_test_function_names(
        (
            ASSISTANT_RUNTIME_TEST_PATH,
            ASSISTANT_EVIDENCE_TEST_PATH,
            ASSISTANT_AGENT_EVAL_TEST_PATH,
        )
    )
    missing: dict[str, list[str]] = {}
    for item, expected_tests in P2_EVIDENCE_TRACE_OWNERSHIP_EVIDENCE.items():
        absent = sorted(test_name for test_name in expected_tests if test_name not in test_names)
        if absent:
            missing[item] = absent

    assert missing == {}


def test_release_test_plan_maps_config_validator_changes_to_config_gate() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=["src/application/config_validator.py"],
        mode="standard",
        version="1.2.184",
    )

    assert {rule["name"] for rule in plan["matched_rules"]} >= {"config", "dependency_graph"}
    assert (
        "python3 -m pytest tests/test_config_yaml.py tests/test_layered_config.py "
        "tests/test_validate_config_notifications.py"
    ) in plan["commands"]


def test_release_test_plan_full_mode_always_adds_full_pytest() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(changed_files=["docs/RELEASE_PROCESS.md"], mode="full")

    assert plan["mode"] == "full"
    assert plan["risk"] == "full"
    assert plan["requires_full_pytest"] is True
    assert plan["commands"][-1] == "python3 -m pytest"


def test_changed_files_from_git_unions_base_staged_and_worktree(tmp_path: Path) -> None:
    from src.application.release_test_plan import changed_files_from_git

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        if command == ["git", "diff", "--name-only", "origin/main...HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="src/application/events/source_futu.py\n", stderr="")
        if command == ["git", "diff", "--name-only", "--cached"]:
            return subprocess.CompletedProcess(command, 0, stdout="docs/RELEASE_PROCESS.md\n", stderr="")
        if command == ["git", "diff", "--name-only"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="src/application/events/source_futu.py\nsrc/interfaces/cli/main.py\n",
                stderr="",
            )
        if command == ["git", "ls-files", "--others", "--exclude-standard"]:
            return subprocess.CompletedProcess(command, 0, stdout="src/application/release_test_plan.py\n", stderr="")
        raise AssertionError(command)

    assert changed_files_from_git(base_ref="origin/main", run_cmd=_run_cmd, cwd=tmp_path) == [
        "docs/RELEASE_PROCESS.md",
        "src/application/events/source_futu.py",
        "src/application/release_test_plan.py",
        "src/interfaces/cli/main.py",
    ]


def test_release_test_plan_rejects_unknown_mode() -> None:
    from src.application.release_test_plan import build_release_test_plan

    with pytest.raises(ValueError, match="unsupported release test mode"):
        build_release_test_plan(changed_files=[], mode="overnight")
