"""Controller-owned integration: actual adapter -> sealed snapshot -> explanation/Brief text."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json

import pandas as pd
import pytest

from candidate_evidence_helpers import earnings_evidence
from domain.domain.insurance_underwriting import InsuranceUnderwritingConfig
from om_strategy_core import CurrencyConverter, ExchangeRates, resolve_strategy
from src.application.scan_sell_put import compute_metrics
from src.application.candidate_models import CandidateContractInput
from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
from src.application.report_summaries import summarize_sell_put
from src.application.opening_candidate_snapshot import OpeningCandidateSnapshotError, validate_opening_candidate_snapshot
from test_candidate_engine_phase2_contract import _opening_row
from test_opening_candidate_snapshot import _seal_snapshot, _scan_status, _publish_opening_manifest
from test_sell_put_strategy_risk import _account_nvda_context

NOW = datetime(2026, 8, 6, 15, tzinfo=timezone.utc)
CONVERTER = CurrencyConverter(ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.920000046))


def _rows():
    rows = []
    for symbol, premium in (("NVDA", 5.0), ("AMD", 3.0)):
        for strike in (100.0, 99.0):
            raw = _opening_row(
                symbol=symbol, stock_owner=f"US.{symbol}", strike=strike,
                contract_symbol=f"US.{symbol}260918P{int(strike * 1000):08d}",
                bid=premium, ask=premium + 0.01,
                snapshot_received_at_utc=NOW.isoformat(), decision_now_utc=NOW.isoformat(),
            )
            contract = CandidateContractInput.from_row(raw, mode="put")
            metrics = compute_metrics(contract, now_utc=NOW)
            assert metrics is not None
            rows.append({
                **raw, **metrics,
                **earnings_evidence(expiration="2026-09-18", market_date="2026-08-06"),
                "cash_free_effective_native": 100000.0,
                "cash_required_native": strike * 100,
                "cash_native_currency": "USD", "max_new_contracts": 10,
            })
    return rows


def _pipeline(base, profile):
    from cash_evidence_helpers import cash_portfolio
    from test_daily_decision_brief_service import _account_dir, _write_close_report
    account_dir = _account_dir(base)
    state = account_dir / "state"
    portfolio = cash_portfolio({
        "as_of_utc": NOW.isoformat(), "cash_by_currency": {"USD": 100000.0},
    }, account_id="12345")
    (state / "portfolio_context.json").write_text(json.dumps(portfolio))
    (state / "option_positions_context.json").write_text(json.dumps({
        "as_of_utc": NOW.isoformat(), "decision_snapshot_status": "trusted",
        "cash_secured_total_by_ccy": {"USD": 0.0},
        "cash_secured_unavailable_by_symbol": {}, "cash_secured_total_cny": 0.0,
        "exchange_rates": {"rates": {"USDCNY": 1 / 0.14, "HKDCNY": 0.920000046},
                           "timestamp": NOW.isoformat()},
        "filters": {"account": "lx"},
    }))
    _write_close_report(account_dir, [])
    policy = resolve_strategy(
        mode="put", config=InsuranceUnderwritingConfig(min_dte=7, max_dte=60),
        ranking_profile=profile, account="lx", market="us",
    )
    from test_portfolio_assignment_scenario import _quote
    context = _account_nvda_context()
    context["position_sizing_evidence"]["quotes"].append(
        _quote("AMD", currency="USD", price=110.0, cny_price=110.0 / 0.14, fx=1 / 0.14)
    )
    rows, decisions, summaries = [], [], []
    for symbol in ("NVDA", "AMD"):
        per_symbol = replace(policy, config=replace(policy.config, max_strike=110.0))
        filtered = enrich_and_filter_sell_put_underwriting(
            df_labeled=pd.DataFrame([r for r in _rows() if r["symbol"] == symbol]),
            symbol=symbol, sell_put_cfg={"strategy": "insurance_underwriting", "max_strike": 110.0},
            portfolio_ctx=context, exchange_rate_converter=CONVERTER,
            decision_sink_fn=decisions.extend, resolved_strategy=per_symbol,
        )
        assert len(filtered) == 2
        rows.extend(filtered.to_dict("records"))
        summaries.append(summarize_sell_put(filtered, symbol))
    snapshot = _seal_snapshot(
        base, run_id="run-1",
        scan_statuses=[_scan_status(s, "put", "completed", snapshot_id="c" * 64,
                                   receipt=f"quotes/{s}/receipt.json") for s in ("NVDA", "AMD")],
        final_candidates={"put": rows}, candidate_evaluations={"put": decisions},
    )
    _publish_opening_manifest(base, snapshot)
    return snapshot, summaries


def _consumer_outputs(base, snapshot):
    from src.application.agent_tools.candidate_rank_impl import candidate_rank_explain_tool
    from src.application.daily_decision_brief_service import assemble_daily_decision_brief
    from src.application.daily_decision_brief_renderer import render_fixed_report
    from test_daily_decision_brief_service import _config, _result

    explained, _, _ = candidate_rank_explain_tool(
        {"runtime_root": str(base), "run_id": "run-1", "account": "lx", "mode": "put"},
        repo_base=lambda: base, resolve_output_root=lambda _: base, mask_path=str,
    )
    brief = assemble_daily_decision_brief(
        base=base, run_id="run-1", account="lx", market="US",
        scheduler_decision={"in_run_window": True}, account_result=_result(),
        pipeline_succeeded=True, config=_config(), now_utc=NOW,
        opening_candidate_snapshot=snapshot,
    )
    text = render_fixed_report(brief)
    return explained, brief, text


def test_experiment_survives_snapshot_brief_and_notification_and_reverts(tmp_path):
    outputs = {}
    for label, profile in (("default", "current_tie_break"), ("experiment", "concentration_first"),
                           ("restored", "current_tie_break")):
        base = tmp_path / label
        snapshot, summaries = _pipeline(base, profile)
        validate_opening_candidate_snapshot(snapshot, expected_run_id="run-1", expected_account="lx")
        order = [r["facts"]["symbol"] for r in snapshot["ranked_candidates"]]
        assert order[:2] == (["AMD", "NVDA"] if label == "experiment" else ["NVDA", "AMD"])
        assert len(snapshot["ranked_candidates"]) == 4
        # Changing cross-symbol ranking must not change either symbol's representative.
        representatives = {r["facts"]["symbol"]: r["facts"]["contract_symbol"]
                           for r in snapshot["ranked_candidates"][:2]}
        assert set(representatives) == {"NVDA", "AMD"}
        explained, brief, text = _consumer_outputs(base, snapshot)
        candidates = brief["candidates"]["sell_put"]
        assert [r["symbol"] for r in candidates][:2] == order[:2]
        assert text.index(order[0]) < text.index(order[1])
        assert [r["symbol"] for r in explained["ranked"]][:2] == order[:2]
        assert all(r["ranking_profile"] == profile for r in explained["ranked"])
        outputs[label] = (representatives, summaries, candidates, text)
    assert outputs["default"][0] == outputs["experiment"][0] == outputs["restored"][0]
    assert outputs["default"] == outputs["restored"]


def test_strategy_identity_is_inside_snapshot_hash(tmp_path):
    snapshot, _ = _pipeline(tmp_path, "concentration_first")
    corrupt = deepcopy(snapshot)
    facts = corrupt["ranked_candidates"][0]["facts"]
    facts["strategy_core"]["ranking_profile"] = "current_tie_break"
    with pytest.raises(OpeningCandidateSnapshotError):
        validate_opening_candidate_snapshot(corrupt, expected_run_id="run-1", expected_account="lx")
