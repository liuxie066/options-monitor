from __future__ import annotations

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.analysis import ANALYSIS_CATALOG_TOOL, ANALYSIS_QUERY_TOOL, _execute_select
from src.application.assistant.answer_verifier import verify_response_against_evidence
from src.application.assistant.evidence import build_evidence_bundle


def test_analysis_query_rejects_write_sql_before_context_access() -> None:
    with pytest.raises(AgentToolError) as exc:
        ANALYSIS_QUERY_TOOL.call(object(), {"sql": "delete from monthly_income_return_summary"})

    assert exc.value.code == "PERMISSION_DENIED"


def test_analysis_catalog_rejects_non_string_view_filter_before_context_access() -> None:
    with pytest.raises(AgentToolError) as exc:
        ANALYSIS_CATALOG_TOOL.call(object(), {"views": {"monthly_income_return_summary": True}})

    assert exc.value.code == "INPUT_ERROR"


def test_analysis_query_authorizer_rejects_non_whitelisted_tables() -> None:
    with pytest.raises(AgentToolError) as exc:
        _execute_select(
            "select name from sqlite_master",
            {"monthly_income_return_summary": [{"month": "2026-05", "account": "lx"}]},
            limit=10,
        )

    assert exc.value.code == "INPUT_ERROR"
    assert "prohibited" in exc.value.message


def test_analysis_query_authorizer_rejects_non_whitelisted_functions() -> None:
    with pytest.raises(AgentToolError) as exc:
        _execute_select(
            "select load_extension('x') as loaded from monthly_income_return_summary",
            {"monthly_income_return_summary": [{"month": "2026-05", "account": "lx"}]},
            limit=10,
        )

    assert exc.value.code == "INPUT_ERROR"
    assert "load_extension" in exc.value.message


def test_analysis_query_executes_read_only_aggregates() -> None:
    rows, columns, views_used = _execute_select(
        (
            "select month, "
            "sum(case when account = 'lx' then net_income_cny else 0 end) as lx_income_cny, "
            "sum(case when account = 'sy' then net_income_cny else 0 end) as sy_income_cny, "
            "sum(case when account = 'lx' then net_income_cny else 0 end) - "
            "sum(case when account = 'sy' then net_income_cny else 0 end) as income_diff_cny "
            "from monthly_income_return_summary group by month"
        ),
        {
            "monthly_income_return_summary": [
                {"month": "2026-05", "account": "lx", "net_income_cny": 35842.0},
                {"month": "2026-05", "account": "sy", "net_income_cny": 23973.0},
            ]
        },
        limit=10,
    )

    assert columns == ["month", "lx_income_cny", "sy_income_cny", "income_diff_cny"]
    assert rows == [
        {
            "month": "2026-05",
            "lx_income_cny": 35842.0,
            "sy_income_cny": 23973.0,
            "income_diff_cny": 11869.0,
        }
    ]
    assert views_used == ["monthly_income_return_summary"]


def test_analysis_query_cells_become_answer_guard_evidence() -> None:
    bundle = build_evidence_bundle(
        question="对比 lx 和 sy 的账户收益，有什么不同？",
        plan={"goal": "对比账户收益", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select ..."},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v1",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [
                        {
                            "month": "2026-05",
                            "account": "lx",
                            "symbol": "FUTU",
                            "net_income_cny": 35842.0,
                            "income_diff_cny": 11869.0,
                        }
                    ],
                    "row_count": 1,
                },
            }
        ],
    )

    payload = bundle.public_payload()
    assert any(item["path"] == "rows[].income_diff_cny" and item["currency"] == "CNY" for item in payload["facts"])

    result = verify_response_against_evidence(
        "2026-05 lx 更高，净现金流 CNY 35,842，差额 CNY 11,869。HK 市场只是说明文字。",
        evidence_bundle=bundle,
    )

    assert result.violations == ()
