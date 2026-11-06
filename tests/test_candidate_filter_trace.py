from __future__ import annotations


def test_symbol_resolve_tool_maps_name_alias_to_canonical_symbol() -> None:
    from src.application.tool_execution import execute_tool as run_tool

    out = run_tool("symbol_resolve", {"symbol": "泡泡玛特"})

    assert out["ok"] is True
    assert out["data"]["resolved"] is True
    assert out["data"]["raw_input"] == "泡泡玛特"
    assert out["data"]["canonical_symbol"] == "9992.HK"
    assert out["data"]["market"] == "HK"
    assert out["data"]["currency"] == "HKD"
    assert out["data"]["futu_code"] == "HK.09992"


def test_candidate_filter_examples_mark_own_incomplete_date_without_copying_grid(tmp_path) -> None:
    from tests.candidate_evidence_helpers import seal_opening_candidate_fixture
    from tests.test_daily_decision_brief_service import _partial_range_provenance
    from src.application.tool_execution import execute_tool

    provenance = _partial_range_provenance()
    seal_opening_candidate_fixture(tmp_path, run_id="run-ranges", rejected_rows=[{
        "symbol": "NVDA", "contract_symbol": "NVDA_OBSERVED", "expiration": "2026-08-21",
        "strike": 100.0, "rule": "risk_spread", **provenance,
    }])
    result = execute_tool("candidate_filter_explain", {
        "runtime_root": str(tmp_path), "account": "lx", "run_id": "run-ranges",
        "symbol": "NVDA", "strategy": "sell_put",
    })
    assert result["ok"], result
    data = result["data"]
    event = next(item for group in data["functions"] for item in group["events"]
                 if item["contract_symbol"] == "NVDA_OBSERVED")
    example = next(item for group in data["summary"] for item in group["examples"]
                   if item["contract_symbol"] == "NVDA_OBSERVED")
    assert event["required_data_coverage"] == provenance["required_data_coverage"]
    for projection in (event, example):
        assert projection["expiration"] == "2026-08-21"
        for key in ("required_data_expiration_status", "ranking_complete", "ranking_scope", "partial_candidate_policy"):
            assert projection[key] == provenance[key]
    assert "required_data_coverage" not in example
    assert "required_data_coverage" not in data["summary"][0]
    assert event["status"] == "rejected"
    assert event["rule"] == "risk_spread"
