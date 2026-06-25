from __future__ import annotations

from src.application.agent_tool_contracts import build_response
from src.application.assistant.model_events import (
    ModelFinalAnswerEvent,
    ToolGuardDecisionEvent,
    adapt_tool_result,
)
from src.application.assistant.model_evidence import (
    build_model_evidence_bundle,
    canonical_fallback_from_tool_results,
    event_observation_from_tool_result,
    verify_model_final_answer,
)


def _income_adapter(*, missing_data: list[dict] | None = None):
    guard = ToolGuardDecisionEvent(
        event_id="guard_income_1",
        tool_call_id="call_income_1",
        tool_name="monthly_income_report",
        allowed=True,
        decision="allow",
        reason="read_auto_in_scope",
        risk_class="READ_AUTO",
        scope_source="host_task_contract",
        normalized_payload={"account": "lx", "month": "2026-06", "include_rows": False, "config_key": "us"},
    )
    data = {
        "filters": {"account": "lx", "month": "2026-06"},
        "summary": [
            {
                "month": "2026-06",
                "account": "lx",
                "currency": "USD",
                "net_cashflow_gross": 123.45,
                "realized_pnl_gross": 10.0,
                "open_basis_lifecycle_pnl_gross": 0.0,
            }
        ],
        "return_summary": [
            {
                "month": "2026-06",
                "account": "lx",
                "net_income_cny": 888.88,
                "net_income_by_ccy": {"USD": 123.45},
                "net_return_rate": 0.0123,
                "cash_collateral_cny": 72266.67,
            }
        ],
        "row_count": 1,
    }
    if missing_data is not None:
        data["missing_data"] = missing_data
    return adapt_tool_result(
        event_id="result_income_1",
        parent_event_id=guard.event_id,
        tool_call_id="call_income_1",
        tool_name="monthly_income_report",
        normalized_payload=guard.normalized_payload,
        guard_decision=guard,
        raw_result=build_response(tool_name="monthly_income_report", ok=True, data=data),
    )


def _analysis_adapter():
    guard = ToolGuardDecisionEvent(
        event_id="guard_analysis_1",
        tool_call_id="call_analysis_1",
        tool_name="analysis_query",
        allowed=True,
        decision="allow",
        reason="read_auto_in_scope",
        risk_class="READ_AUTO",
        scope_source="host_task_contract",
        normalized_payload={
            "config_key": "us",
            "sql": "select month, account, net_income_cny from account_monthly_performance where month = '2026-06'",
            "limit": 20,
        },
    )
    data = {
        "schema_version": "analysis.query.output.v2",
        "source_label": "OM read-only analysis workspace",
        "columns": ["month", "account", "net_income_cny"],
        "rows": [
            {"month": "2026-06", "account": "lx", "net_income_cny": 2414.0},
            {"month": "2026-06", "account": "sy", "net_income_cny": 11138.0},
        ],
        "row_count": 2,
        "truncated": False,
        "views_used": ["account_monthly_performance"],
        "fallback_text": (
            "分析查询结果：2 行\n"
            "| month | account | net_income_cny |\n"
            "| --- | --- | --- |\n"
            "| 2026-06 | lx | 2,414 |\n"
            "| 2026-06 | sy | 11,138 |\n"
            "数据来源：OM read-only analysis workspace"
        ),
    }
    return adapt_tool_result(
        event_id="result_analysis_1",
        parent_event_id=guard.event_id,
        tool_call_id="call_analysis_1",
        tool_name="analysis_query",
        normalized_payload=guard.normalized_payload,
        guard_decision=guard,
        raw_result=build_response(tool_name="analysis_query", ok=True, data=data),
    )


def _analysis_catalog_adapter():
    guard = ToolGuardDecisionEvent(
        event_id="guard_analysis_catalog_1",
        tool_call_id="call_analysis_catalog_1",
        tool_name="analysis_catalog",
        allowed=True,
        decision="allow",
        reason="read_auto_in_scope",
        risk_class="READ_AUTO",
        scope_source="host_task_contract",
        normalized_payload={"config_key": "us"},
    )
    data = {
        "view_count": 1,
        "views": {
            "account_monthly_performance": {
                "description": "monthly account performance",
                "fields": ["month", "account", "net_income_cny"],
                "recommended_filters": ["month", "account"],
            }
        },
        "sql_rules": {"allowed_statements": ["SELECT", "WITH"], "writes_allowed": False},
    }
    return adapt_tool_result(
        event_id="result_analysis_catalog_1",
        parent_event_id=guard.event_id,
        tool_call_id="call_analysis_catalog_1",
        tool_name="analysis_catalog",
        normalized_payload=guard.normalized_payload,
        guard_decision=guard,
        raw_result=build_response(tool_name="analysis_catalog", ok=True, data=data),
    )


def test_event_tool_result_builds_existing_evidence_bundle_with_output_contract() -> None:
    adapter = _income_adapter()

    model_evidence = build_model_evidence_bundle(
        question="6月收益分析",
        task_contract={"goal": "分析 6 月收益", "scope": {"requested_accounts": ["lx"]}},
        tool_results=[adapter],
        parent_event_id=adapter.event.event_id,
    )

    observation = model_evidence.observations[0]
    trace = model_evidence.evidence_bundle.trace_payload()
    assert model_evidence.evidence_event.event_type == "evidence_updated"
    assert observation["output_contract"]["canonical_renderer"] == "monthly_income"
    assert trace["fact_count"] >= 4
    assert trace["dataset_count"] == 1
    assert trace["tools"] == ["monthly_income_report"]
    assert "income_summary" in trace["guard_profiles"]


def test_event_final_answer_verifier_passes_supported_claims_and_fails_unsupported_amount() -> None:
    adapter = _income_adapter()
    model_evidence = build_model_evidence_bundle(
        question="6月收益分析",
        task_contract={"goal": "分析 6 月收益", "scope": {"requested_accounts": ["lx"]}},
        tool_results=[adapter],
    )
    supported = ModelFinalAnswerEvent(
        event_id="answer_1",
        parent_event_id=model_evidence.evidence_event.event_id,
        answer_text="6月 lx 净现金流为 USD 123.45。",
    )
    unsupported = ModelFinalAnswerEvent(
        event_id="answer_2",
        parent_event_id=model_evidence.evidence_event.event_id,
        answer_text="6月 lx 净现金流为 USD 999.99。",
    )

    ok = verify_model_final_answer(
        answer_event=supported,
        model_evidence=model_evidence,
        tool_results=[adapter],
    )
    bad = verify_model_final_answer(
        answer_event=unsupported,
        model_evidence=model_evidence,
        tool_results=[adapter],
    )

    assert ok.passed is True
    assert ok.status == "passed"
    assert bad.passed is False
    assert bad.status == "failed"
    assert bad.fallback_text
    assert any(item["type"] == "unsupported_contract_currency_amount" for item in bad.guard["violations"])
    assert bad.trace["fallback"] == "canonical_renderer"


def test_event_evidence_carries_missing_data_records_from_tool_result_event() -> None:
    adapter = _income_adapter(
        missing_data=[{"kind": "ledger_gap", "impact": "income rows are incomplete", "recoverable_by": "analysis_query"}]
    )

    model_evidence = build_model_evidence_bundle(
        question="6月收益分析",
        task_contract={"goal": "分析 6 月收益"},
        tool_results=[adapter],
    )

    missing = model_evidence.evidence_bundle.public_payload()["missing_data"]
    assert any(item["kind"] == "ledger_gap" for item in missing)
    assert any(item["source_tool"] == "monthly_income_report" for item in missing)


def test_event_final_answer_verifier_rejects_claim_that_observed_analysis_rows_are_hidden() -> None:
    adapter = _analysis_adapter()
    model_evidence = build_model_evidence_bundle(
        question="6月收益总结",
        task_contract={"goal": "总结 2026-06 收益", "scope": {"months": ["2026-06"]}},
        tool_results=[adapter],
    )
    bad = ModelFinalAnswerEvent(
        event_id="answer_bad_analysis_rows",
        parent_event_id=model_evidence.evidence_event.event_id,
        answer_text="查询返回的 rows 被截断未展示具体数值，所以我无法给出具体金额。",
    )

    verification = verify_model_final_answer(
        answer_event=bad,
        model_evidence=model_evidence,
        tool_results=[adapter],
    )

    assert verification.passed is False
    assert verification.status == "failed"
    assert any(item["type"] == "contradicts_observed_analysis_rows" for item in verification.guard["violations"])
    assert "2,414" in verification.fallback_text


def test_event_observation_uses_normalized_payload_for_payload_dependent_contract() -> None:
    adapter = _income_adapter()

    observation = event_observation_from_tool_result(adapter, index=1)

    assert observation["payload"]["include_rows"] is False
    assert observation["output_contract"]["schema_version"] == "monthly_income_report.output.v1"


def test_canonical_fallback_prefers_existing_renderer() -> None:
    adapter = _income_adapter()

    fallback = canonical_fallback_from_tool_results([adapter])

    assert fallback
    assert "USD" in fallback


def test_canonical_fallback_skips_internal_catalog_renderer() -> None:
    adapter = _analysis_catalog_adapter()

    fallback = canonical_fallback_from_tool_results([adapter])

    assert fallback == ""
