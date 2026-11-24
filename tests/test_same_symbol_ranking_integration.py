"""Frozen group -> ordering -> sealed explanation -> read-only user projections."""
from copy import deepcopy

import pandas as pd
import pytest

from domain.domain.strategy_core import evaluate_candidate_decision, validate_strategy_identity
from src.application import strategy_sample_verify
from src.application.daily_decision_brief_service import _candidate_view
from src.application.daily_decision_brief_renderer import _candidate_metric_details
from src.application.opening_candidate_snapshot import ranked_opening_candidates, OpeningCandidateSnapshotError
from src.application.report_summaries import summarize_sell_put, summarize_sell_call
from test_strategy_sample_verify import candidate, seal
from test_same_symbol_ranking import _rows


@pytest.mark.parametrize("summarize", [summarize_sell_put, summarize_sell_call])
def test_legacy_summary_does_not_require_contract_or_strategy_identity(summarize):
    summary = summarize(pd.DataFrame([{"symbol": "NVDA", "strike": 100, "currency": "USD"}]), "NVDA")
    assert summary["candidate_count"] == 1
    assert "ranking" not in summary


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("missing", [False, True])
def test_full_group_seal_summary_offline_and_brief_agree(tmp_path, mode, missing):
    records = []
    for strike, donor in zip((90, 95), (_rows(mode)[0], _rows(mode)[2])):
        row, _ = candidate(mode, strike=strike)
        # Accepted fixtures exercise a supplied net price, not a new quote owner.
        for field in ("breakeven", "net_sale_price", "same_symbol_ranking_context", "period_net_return"):
            if field in donor:
                row[field] = deepcopy(donor[field])
        row["period_net_return_on_cash_basis" if mode == "put" else "period_net_premium_return"] = donor["period_net_return"]
        if missing and strike == 95:
            row.pop("breakeven" if mode == "put" else "net_sale_price", None)
        decision = evaluate_candidate_decision(row, strategy=validate_strategy_identity(row, mode=mode))["opening_decision"]
        assert decision["accepted"]
        records.append((decision["normalized_input"], decision))
    before = deepcopy(records)
    payload = seal(tmp_path, records, mode)
    assert records == before
    ranked = ranked_opening_candidates(payload, mode=mode)
    target_id = records[0][0]["contract_symbol"]
    target = next(item for item in ranked if item["facts"]["contract_symbol"] == target_id)
    tier = target["ranking"]["net_delivery_tier"]
    assert tier["qualifies"] is True
    assert tier["priority"] is (not missing)
    assert ranked[0]["facts"]["contract_symbol"] == records[1 if missing else 0][0]["contract_symbol"]
    summarize = summarize_sell_put if mode == "put" else summarize_sell_call
    summary = summarize(pd.DataFrame([r for r, _ in records]), "NVDA")
    assert summary["ranking"]["net_delivery_tier"] == ranked[0]["ranking"]["net_delivery_tier"]
    from src.application.agent_tools.candidate_rank_impl import candidate_rank_explain_tool
    page, _, _ = candidate_rank_explain_tool(
        {"account": "lx", "run_id": "parity", "mode": mode, "top_n": 1},
        repo_base=lambda: tmp_path, resolve_output_root=lambda _: tmp_path, mask_path=str,
    )
    assert page["returned_count"] == 1
    assert page["ranked_summary"][0]["net_delivery_tier"] == ranked[0]["ranking"]["net_delivery_tier"]
    checks = []
    strategy_sample_verify._ranking_checks(payload, {d["candidate_id"] for d in payload["candidate_decisions"]}, checks)
    assert checks and all(check["status"] == "matched" for check in checks), checks
    family = "sell_put" if mode == "put" else "covered_call"
    view = _candidate_view({**target["facts"], "ranking": target["ranking"]}, family=family, rank=2)
    assert view["metrics"]["net_delivery_tier"] == tier
    text = "\n".join(_candidate_metric_details(view, family=family, market="US"))
    assert ("本组候选净价不完整" if missing else "新档位优先") in text
    assert "仅计开仓费用" in text
    if missing:
        assert "新档位优先" not in text
    # Explanation drift is detected independently of a still-correct ordering.
    target_record = next(r for r in payload["ranked_candidates"] if r["candidate_id"] == target["candidate_id"])
    target_record["ranking"]["net_delivery_tier"]["priority"] = missing
    checks = []
    strategy_sample_verify._ranking_checks(payload, {d["candidate_id"] for d in payload["candidate_decisions"]}, checks)
    assert any(check["category"] == "ranking_explanation" and check["status"] == "mismatched" for check in checks)


def test_final_only_context_is_rejected_by_snapshot(tmp_path):
    row, decision = candidate()
    row = deepcopy(row)
    row["same_symbol_ranking_context"] = _rows("put")[0]["same_symbol_ranking_context"]
    from test_opening_candidate_snapshot import _seal_snapshot, _scan_status
    with pytest.raises(OpeningCandidateSnapshotError, match="context mismatch"):
        _seal_snapshot(tmp_path, run_id="mismatch", scan_statuses=[_scan_status("NVDA", "put", "completed")],
                       final_candidates={"put": [row]}, candidate_evaluations={"put": [
                           {"normalized_input": decision["normalized_input"], "opening_decision": decision}]})


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("state", ["available", "unavailable", "old_cache"])
def test_underwriting_freezes_price_context_in_decision_and_final_rows(mode, state):
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_underwriting
    from test_strategy_core_integration import _portfolio, CONVERTER
    row, _ = candidate(mode)
    strategy = validate_strategy_identity(row, mode=mode)
    context = _portfolio()
    context["option_ctx"].update(
        filters={"account": "lx", "broker": "富途"}, decision_state_fingerprint="frozen-ledger",
        ordinary_opening_prices={"status": "available", "by_symbol": {"NVDA": {mode: {
            "status": state, "reason": None if state == "available" else "actual_opening_fee_unavailable",
            "currency": "USD", "prices": [{"lot_id": "existing", "net_price": 100}],
        }}}},
    )
    if state == "old_cache":
        context["option_ctx"].pop("ordinary_opening_prices")
    decisions = []
    kwargs = {"df_labeled": pd.DataFrame([row]), "symbol": "NVDA", "portfolio_ctx": context,
              "exchange_rate_converter": CONVERTER, "resolved_strategy": strategy,
              "decision_sink_fn": decisions.extend}
    if mode == "put":
        result = enrich_and_filter_sell_put_underwriting(**kwargs, sell_put_cfg={})
    else:
        result = enrich_and_filter_covered_call_underwriting(**kwargs, sell_call_cfg={})
    assert len(result) == 1 and len(decisions) == 1
    frozen = decisions[0]["normalized_input"]["same_symbol_ranking_context"]
    assert frozen == result.iloc[0]["same_symbol_ranking_context"]
    assert frozen["status"] == ("unavailable" if state == "old_cache" else state)
    assert frozen["decision_state_fingerprint"] == "frozen-ledger"
    assert context["option_ctx"]["decision_state_fingerprint"] == "frozen-ledger"
