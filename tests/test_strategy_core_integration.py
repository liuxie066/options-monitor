"""S2 ordinary source adapters: frozen facts only, no provider or runtime access."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from domain.domain.engine import rank_candidate_rows
from domain.domain.engine.candidate_engine import explain_candidate_rank
from domain.domain.insurance_underwriting import InsuranceUnderwritingConfig, rank_underwriting_candidates
from domain.domain.strategy_core import evaluate_candidates, resolve_strategy
from src.application import scan_sell_call, scan_sell_put
from src.application.candidate_models import CandidateContractInput
from src.application.candidate_scanning import attach_strategy_identity
from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_underwriting
from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
from src.application.sell_put_steps import run_sell_put_scan_and_summarize
from src.application.sell_call_steps import run_sell_call_scan_and_summarize
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
from cash_evidence_helpers import cash_portfolio
from test_cc_spot_distance_contract import NOW, _quote, _write_calendar
from test_portfolio_assignment_scenario import _evidence, _holding, _quote as valuation_quote

GOLDEN = json.loads((Path(__file__).parent / "fixtures/strategy_core_baseline.json").read_text())
CONVERTER = CurrencyConverter(ExchangeRates(.14, .920000046))


def _strategy(mode="put", **changes):
    return resolve_strategy(mode=mode, config=InsuranceUnderwritingConfig(min_dte=7, max_dte=60),
                            account="lx", market="us", **changes)


def _without_identity(value):
    if isinstance(value, dict):
        return {key: _without_identity(item) for key, item in value.items() if key != "strategy_core"}
    if isinstance(value, list):
        return [_without_identity(item) for item in value]
    return value


@pytest.fixture
def frozen_scan(tmp_path, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(scan_sell_put, "datetime", FixedDatetime)
    monkeypatch.setattr(scan_sell_call, "datetime", FixedDatetime)
    _write_calendar(tmp_path)
    return tmp_path


def _portfolio():
    evidence = _evidence(
        holdings=[_holding("CNY-CASH", "现金", "cash", quantity=1_000_000, market_value_cny=1_000_000),
                  _holding("NVDA", "NVDA", "stock", quantity=500, market_value_cny=500 * 100 / .14,
                           currency="USD", normalized_type="stock")],
        quotes=[valuation_quote(s, currency="USD", price=100, cny_price=100 / .14, fx=1 / .14)
                for s in ("NVDA", "MSFT")],
    )
    return cash_portfolio({
        "filters": {"account": "lx"}, "cash_by_currency": {"USD": 100_000},
        "position_sizing_evidence": evidence,
        "option_ctx": {"context_status": "available", "decision_snapshot_status": "trusted",
                       "cash_secured_by_symbol_by_ccy": {}, "cash_secured_total_by_ccy": {},
                       "assignment_positions": []},
    })


def _put_quote(symbol, strike, bid):
    return {**_quote(symbol, strike), "option_type": "put", "delta": -0.3,
            "contract_symbol": f"{symbol}261113P{int(strike * 1000):08d}", "bid": bid, "ask": bid + .1}


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["name"])
def test_metrics_delegate_without_changing_original_numbers_or_rejections(case, monkeypatch):
    adapter = scan_sell_put if case["mode"] == "put" else scan_sell_call
    core = adapter.calculate_metrics
    calls = []

    def tracked(row, **kwargs):
        assert kwargs["now_utc"].utcoffset() is not None
        calls.append(kwargs)
        return core(row, **kwargs)

    monkeypatch.setattr(adapter, "calculate_metrics", tracked)
    contract = CandidateContractInput.from_row(case["row"], mode=case["mode"])
    args = () if case["mode"] == "put" else (case["avg_cost"],)
    now = datetime.fromisoformat(case["now_utc"])
    actual = adapter.compute_metrics(contract, *args, now_utc=now)
    if "error" in case:
        assert actual is None
        assert adapter.explain_metrics_rejection(contract, *args, now_utc=now) == case["error"]
    else:
        assert {key: actual[key] for key in case["metrics"]} == case["metrics"]
    assert calls


@pytest.mark.parametrize("cost", [None, 0, -1, float("nan"), True])
def test_bad_cc_cost_keeps_metric_boundary_and_stale_first_rejection(cost):
    raw = _quote()
    assert scan_sell_call.compute_metrics(pd.Series(raw), cost, now_utc=NOW) is None
    assert scan_sell_call.explain_metrics_rejection(pd.Series(raw), cost, now_utc=NOW)["rule"] == "avg_cost_invalid"
    stale = {**raw, "snapshot_received_at_utc": "2020-01-01T00:00:00Z"}
    expected = scan_sell_call.explain_metrics_rejection(pd.Series(stale), 100, now_utc=NOW)
    assert expected["rule"] != "avg_cost_invalid"
    assert scan_sell_call.explain_metrics_rejection(pd.Series(stale), cost, now_utc=NOW) == expected


def test_legacy_optional_time_and_demo_cost_sources(frozen_scan):
    current = datetime.now(timezone.utc)
    fresh = {**_quote(), "snapshot_received_at_utc": current.isoformat(), "spot_observed_at_utc": current.isoformat()}
    assert scan_sell_call.compute_metrics(pd.Series(fresh), 80) is not None
    assert scan_sell_call.compute_metrics(pd.Series(fresh), 80, now_utc=NOW.replace(tzinfo=None)) is not None
    for demo, cost in ((False, 80), (True, 100)):
        frame = scan_sell_call.run_sell_call_scan(
            symbols=["NVDA"], input_root=frozen_scan, avg_cost=80,
            shares=100, shares_can_sell=100, shares_locked=0, min_annualized_net_return=0,
            required_data_frames={"NVDA": pd.DataFrame([_quote()])}, demo_capacity=demo,
            quote_freshness_now_utc=NOW,
        )
        row = frame.iloc[0]
        assert row["avg_cost"] == cost
        assert row["strike_above_cost_pct"] == round((110 - cost) / cost, 10)


def _run_puts(root, *, experimental=False, injected=True, cash_available=True, context=None):
    finals, decisions, summaries = [], [], []
    context = deepcopy(context) if context is not None else _portfolio()
    if not cash_available:
        context["cash_by_currency"] = {"USD": 0}
    for symbol, bid, max_strike in (("NVDA", 3., 99.), ("MSFT", 1.5, 100.)):
        strategy = replace(_strategy(), config=replace(_strategy().config, max_strike=max_strike),
                           ranking_profile="concentration_first" if experimental else "current_tie_break")
        quotes = [_put_quote(symbol, 95, bid), _put_quote(symbol, 96, bid - .5)]
        if symbol == "NVDA":
            quotes.append({**_put_quote(symbol, 97, bid), "snapshot_received_at_utc": "2020-01-01T00:00:00Z"})
        summaries.extend(run_sell_put_scan_and_summarize(
            symbol=symbol, symbol_cfg={}, sp={"strategy": "insurance_underwriting", "max_strike": max_strike},
            required_data_dir=root, required_data_frame=pd.DataFrame(quotes),
            portfolio_ctx=context, exchange_rate_converter=CONVERTER,
            resolved_strategy=strategy if injected else None,
            final_candidates_sink_fn=lambda mode, rows: finals.extend(rows),
            candidate_decisions_sink_fn=lambda mode, rows: decisions.extend(rows),
        ))
    return finals, decisions, summaries


def test_real_put_steps_multi_symbol_rank_complete_passes_and_reversion(frozen_scan):
    context = _portfolio()
    baseline, decisions, summaries = _run_puts(frozen_scan, injected=False, context=context)
    explicit, _, _ = _run_puts(frozen_scan, context=context)
    experiment, exp_decisions, exp_summaries = _run_puts(frozen_scan, experimental=True, context=context)
    # Provenance alone changes; arithmetic, capacity, stage and per-symbol selection do not.
    pd.testing.assert_frame_equal(pd.DataFrame(_without_identity(baseline)), pd.DataFrame(_without_identity(explicit)), check_exact=True)
    pd.testing.assert_frame_equal(pd.DataFrame(_without_identity(baseline)), pd.DataFrame(_without_identity(experiment)), check_exact=True)
    assert len(baseline) == 4 and len(decisions) == 5
    assert [s["candidate_count"] for s in summaries] == [2, 2]
    assert [s["top_contract"] for s in summaries] == [s["top_contract"] for s in exp_summaries]
    assert [r["symbol"] for r in rank_candidate_rows(baseline, mode="put")[:2]] == ["NVDA", "MSFT"]
    ranked = rank_candidate_rows(experiment, mode="put")
    assert [r["symbol"] for r in ranked[:2]] == ["MSFT", "NVDA"]
    assert all(r["strike"] == 95 for r in ranked[:2])
    assert len(ranked) == 4
    result = evaluate_candidates([r for r in experiment if r["symbol"] == "NVDA"],
                                 strategy=replace(_strategy(), config=replace(_strategy().config, max_strike=99),
                                                  ranking_profile="concentration_first"))
    assert len(result["accepted"]) == 2 and len(result["representatives"]) == 1
    rejects = [d for d in decisions if not d["opening_decision"]["accepted"]]
    exp_rejects = [d for d in exp_decisions if not d["opening_decision"]["accepted"]]
    assert _without_identity(rejects) == _without_identity(exp_rejects)
    assert rejects[0]["opening_decision"]["rejects"][0]["stage"] == "stage0_input_normalization"
    for record in exp_decisions:
        assert record["normalized_input"]["strategy_core"] == record["opening_decision"]["normalized_input"]["strategy_core"]
        assert record["normalized_input"]["strategy_core"]["runtime"] == {
            "mode": "source", "core_version": "0.2.0", "build_digest": None,
        }
    assert "先按接货后集中度" in explain_candidate_rank(ranked[0], mode="put")["rank_reason"]
    restored, _, _ = _run_puts(frozen_scan, injected=False, context=context)
    pd.testing.assert_frame_equal(pd.DataFrame(baseline), pd.DataFrame(restored), check_exact=True)
    assert all(row["strategy_core"]["policy_kind"] == "baseline" for row in baseline)
    assert baseline[0]["strategy_core"]["identity_format"] == "strategy_identity.v2"
    assert baseline[0]["strategy_core"]["effective_policy_hash"] != baseline[2]["strategy_core"]["effective_policy_hash"]
    assert baseline[0]["strategy_core"]["strike_inputs"] != baseline[2]["strategy_core"]["strike_inputs"]


def test_cash_rejection_stays_before_return_policy(frozen_scan):
    finals, decisions, _ = _run_puts(frozen_scan, cash_available=False)
    assert not finals
    underwriting = [d for d in decisions if d["opening_decision"]["rejects"][0]["stage"] != "stage0_input_normalization"]
    assert len(underwriting) == 4
    assert all(d["opening_decision"]["rejects"][0]["reason"] == "hard_capacity_put" for d in underwriting)


@pytest.mark.parametrize("mode", ["put", "call"])
def test_injected_policy_wins_over_cfg_and_disabled_legacy_stays_passthrough(mode):
    case = next(c for c in GOLDEN["cases"] if c["mode"] == mode and "policy_row" in c)
    row = {**case["policy_row"], "avg_cost": case["avg_cost"]}
    adapter = enrich_and_filter_sell_put_underwriting if mode == "put" else enrich_and_filter_covered_call_underwriting
    cfgkey = "sell_put_cfg" if mode == "put" else "sell_call_cfg"
    common = dict(df_labeled=pd.DataFrame([row]), symbol=row["symbol"], portfolio_ctx={"filters": {"account": "lx"}},
                  exchange_rate_converter=CONVERTER)
    disabled_decisions = []
    legacy = adapter(**common, **{cfgkey: {"strategy": "legacy"}}, decision_sink_fn=disabled_decisions.extend)
    assert len(legacy) == 1 and "strategy_core" not in legacy and disabled_decisions == []
    strategy = replace(_strategy(mode), config=replace(_strategy(mode).config, min_net_income=1_000_000))
    records = []
    rejected = adapter(**common, **{cfgkey: {"strategy": "legacy", "min_net_income": 0}},
                       resolved_strategy=strategy, decision_sink_fn=records.extend)
    assert rejected.empty
    assert records[0]["normalized_input"]["policy_min_net_premium_cny"] == 1_000_000
    assert records[0]["normalized_input"]["strategy_core"]["effective_policy_hash"] == strategy.identity["effective_policy_hash"]
    for mismatch in (replace(strategy, account="sy"), replace(strategy, market="hk"), _strategy("call" if mode == "put" else "put")):
        with pytest.raises(ValueError):
            adapter(**common, **{cfgkey: {}}, resolved_strategy=mismatch)


@pytest.mark.parametrize("mutation", ["account", "market", "profile", "hash", "runtime", "strike", "missing"])
def test_rank_rejects_mixed_or_forged_identity(mutation):
    row = attach_strategy_identity({"symbol": "NVDA", "option_type": "put", "delta": -0.3, "market": "US"}, _strategy())
    other = deepcopy(row)
    if mutation == "account":
        other["strategy_core"]["scope"]["account"] = "sy"
    elif mutation == "market":
        other["strategy_core"]["scope"]["market"] = "hk"
    elif mutation == "profile":
        other["strategy_core"]["ranking_profile"] = "concentration_first"
    elif mutation == "hash":
        other["strategy_core"]["effective_policy_hash"] = "a" * 64
    elif mutation == "runtime":
        other["strategy_core"]["runtime"]["build_digest"] = "a" * 64
    elif mutation == "strike":
        other["strategy_core"]["strike_inputs"]["min_strike"] = float("nan")
    else:
        other.pop("strategy_core")
    with pytest.raises(ValueError):
        rank_candidate_rows([row, other], mode="put")


def test_explicit_rank_conflict_and_per_symbol_policy_variation():
    strategy = replace(_strategy(), ranking_profile="concentration_first")
    rows = [attach_strategy_identity({"symbol": symbol}, policy) for symbol, policy in (
        ("NVDA", strategy), ("MSFT", replace(strategy, config=replace(strategy.config, min_iv_rv_ratio=1.2))),
    )]
    assert len(rank_candidate_rows(rows, mode="put")) == 2
    with pytest.raises(ValueError, match="explicit ranking"):
        rank_candidate_rows(rows, mode="put", sell_put_ranking_profile="current_tie_break")
    with pytest.raises(ValueError, match="config conflicts"):
        rank_underwriting_candidates(rows, mode="put", cfg=InsuranceUnderwritingConfig())
    with pytest.raises(ValueError, match="mode"):
        rank_candidate_rows(rows, mode="call")


def test_cc_step_uses_injected_cost_floor_and_policy(frozen_scan):
    finals, decisions = [], []
    strategy = replace(_strategy("call"), config=replace(_strategy("call").config, min_strike=115))
    result = run_sell_call_scan_and_summarize(
        symbol="NVDA", symbol_cfg={}, cc={"strategy": "insurance_underwriting", "min_strike": 200},
        stock={"shares": 100, "can_sell_qty": 100, "avg_cost": 80},
        portfolio_ctx={"filters": {"account": "lx"}, "portfolio_source_name": "futu", "capacity_authority": {"status": "available"}},
        locked_shares_status="available", locked_shares_by_symbol={}, locked_shares_unavailable_by_symbol={},
        required_data_dir=frozen_scan, required_data_frame=pd.DataFrame([_quote(strike=120)]),
        exchange_rate_converter=CONVERTER, resolved_strategy=strategy,
        final_candidates_sink_fn=lambda mode, rows: finals.extend(rows),
        candidate_decisions_sink_fn=lambda mode, rows: decisions.extend(rows),
    )
    assert result["candidate_count"] == 1
    assert finals[0]["avg_cost"] == 80
    assert finals[0]["effective_min_strike"] == 115
    assert decisions[0]["normalized_input"]["strategy_core"]["strike_inputs"]["min_strike"] == 115
    assert result["strategy_core"] == finals[0]["strategy_core"]


@pytest.mark.parametrize("multiplier,strike,accepted", [(1.02, 110, True), (1.20, 110, False), (1.20, 125, True)])
@pytest.mark.parametrize("injected", [False, True])
def test_cc_cost_policy_steps_decision_snapshot_and_restoration(frozen_scan, multiplier, strike, accepted, injected):
    from domain.domain.strategy_core import validate_strategy_identity
    from src.application.opening_candidate_snapshot import (
        load_opening_candidate_snapshot, ranked_opening_candidates, ranked_opening_candidate_decisions,
        rejected_opening_candidate_decisions, _snapshot_decisions,
    )
    from test_opening_candidate_snapshot import _seal_snapshot, _scan_status

    policy = replace(_strategy("call"), config=replace(_strategy("call").config, min_strike_cost_multiplier=multiplier))
    finals, decisions = [], []
    summary = run_sell_call_scan_and_summarize(
        symbol="NVDA", symbol_cfg={},
        cc={"strategy": "insurance_underwriting", "min_strike_cost_multiplier": 9.0 if injected else multiplier},
        stock={"shares": 100, "can_sell_qty": 100, "avg_cost": 100},
        portfolio_ctx={"filters": {"account": "lx"}, "portfolio_source_name": "futu", "capacity_authority": {"status": "available"}},
        locked_shares_status="available", locked_shares_by_symbol={}, locked_shares_unavailable_by_symbol={},
        required_data_dir=frozen_scan, required_data_frame=pd.DataFrame([_quote(strike=strike)]),
        exchange_rate_converter=CONVERTER, resolved_strategy=policy if injected else None,
        final_candidates_sink_fn=lambda mode, rows: finals.extend(rows),
        candidate_decisions_sink_fn=lambda mode, rows: decisions.extend(rows),
    )
    assert summary["candidate_count"] == int(accepted)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision["opening_decision"]["accepted"] is accepted
    row = decision["normalized_input"]
    assert row["effective_min_strike"] == 100 * multiplier
    assert row["strategy_core"]["policy_kind"] == ("baseline" if multiplier == 1.02 else "custom")
    assert validate_strategy_identity(row, mode="call") == policy
    if not accepted:
        assert decision["opening_decision"]["rejects"][0]["reason"] == "hard_strike"
    sealed = _seal_snapshot(
        frozen_scan, run_id="cc-cost", scan_statuses=[_scan_status("NVDA", "call", "completed")],
        final_candidates={"call": finals}, candidate_evaluations={"call": decisions},
    )
    loaded = load_opening_candidate_snapshot(base=frozen_scan, run_id="cc-cost", account="lx", require_current_contract=True)
    assert loaded == sealed
    ranked = ranked_opening_candidates(loaded, mode="call")
    assert len(ranked) == int(accepted)
    restored_rows = [item["normalized_input"] for item in (
        ranked_opening_candidate_decisions(loaded) + rejected_opening_candidate_decisions(loaded, mode="call")
    )]
    assert len(restored_rows) == 1
    for restored_row in restored_rows:
        restored = validate_strategy_identity(restored_row, mode="call")
        assert restored == policy
        assert evaluate_candidates([restored_row], strategy=restored)["decisions"][0]["accepted"] is accepted
    assert len(rank_underwriting_candidates(restored_rows, mode="call", cfg=policy.config)) == 1
    if accepted:
        fallback, _ = _snapshot_decisions(
            {"call": finals}, statuses=[_scan_status("NVDA", "call", "completed")],
            account="lx", market="US", futu_account_id="fixture", risk_policy_hash="a" * 64,
            candidate_evaluations=None,
        )
        assert fallback[0]["opening_decision"]["accepted"]
        assert validate_strategy_identity(fallback[0]["normalized_input"], mode="call") == policy


@pytest.mark.parametrize("value", [None, True, 0, -1, "bad", float("nan"), float("inf")])
def test_cc_raw_multiplier_boundary_and_injected_precedence(frozen_scan, value):
    from src.application.covered_call_strategy_risk import resolve_covered_call_underwriting_config
    from test_cc_spot_distance_contract import _metrics

    raw = {"strategy": "insurance_underwriting", "min_strike_cost_multiplier": value}
    with pytest.raises(ValueError, match="min_strike_cost_multiplier"):
        resolve_covered_call_underwriting_config(raw)
    rows = pd.DataFrame([_metrics(cost=100)])
    records = []
    filtered = enrich_and_filter_covered_call_underwriting(
        df_labeled=rows, symbol="NVDA", portfolio_ctx=None, sell_call_cfg=raw,
        exchange_rate_converter=CONVERTER, resolved_strategy=_strategy("call"), decision_sink_fn=records.extend,
    )
    assert len(filtered) == 1
    assert records[0]["normalized_input"]["effective_min_strike"] == 102
    scanned = scan_sell_call.run_sell_call_scan(
        symbols=["NVDA"], input_root=frozen_scan, avg_cost=100,
        shares=100, shares_can_sell=100, shares_locked=0, min_annualized_net_return=0,
        min_strike_cost_multiplier=value, resolved_strategy=_strategy("call"),
        required_data_frames={"NVDA": pd.DataFrame([_quote()])}, quote_freshness_now_utc=NOW,
    )
    assert len(scanned) == 1
    assert scanned.iloc[0]["strategy_core"]["effective_parameters"]["min_strike_cost_multiplier"] == 1.02
    legacy_records = []
    legacy = enrich_and_filter_covered_call_underwriting(
        df_labeled=rows, symbol="NVDA", portfolio_ctx=None, sell_call_cfg={**raw, "strategy": "legacy"},
        exchange_rate_converter=CONVERTER, decision_sink_fn=legacy_records.extend,
    )
    assert len(legacy) == 1 and "strategy_core" not in legacy and legacy_records == []
    assert legacy.iloc[0]["effective_min_strike"] == 102


def test_cc_raw_numeric_string_keeps_existing_normalization():
    from src.application.covered_call_strategy_risk import resolve_covered_call_underwriting_config

    cfg = resolve_covered_call_underwriting_config({"strategy": "insurance_underwriting", "min_strike_cost_multiplier": "1.20"})
    assert cfg.min_strike_cost_multiplier == 1.20
    assert resolve_strategy(mode="call", config=cfg).identity["policy_kind"] == "custom"


@pytest.mark.parametrize("change", ["account", "market", "row_account", "runtime", "mode"])
def test_snapshot_scope_binds_identity_including_uppercase_market(change):
    from src.application.opening_candidate_snapshot import _snapshot_decisions, OpeningCandidateSnapshotError
    from test_opening_candidate_snapshot import _candidate

    row = attach_strategy_identity(_candidate(contract_symbol="NVDA-P", period_return=.03), _strategy())
    if change == "row_account":
        row["account"] = "sy"
    elif change == "runtime":
        row["strategy_core"].pop("runtime")
    else:
        row["strategy_core"]["scope"][change] = {"account": "sy", "market": "hk", "mode": "call"}[change]
    # Scope rejection happens before policy re-evaluation or any persistent effect.
    with pytest.raises(OpeningCandidateSnapshotError):
        _snapshot_decisions({"put": [row]}, statuses=[{"symbol": "NVDA", "strategy_mode": "put"}],
                            account="lx", market="US", futu_account_id="fixture", risk_policy_hash="a" * 64,
                            candidate_evaluations=None)


def test_snapshot_fallback_uses_resolved_policy_and_hashes_identity():
    from domain.domain.decision_state_fingerprint import canonical_sha256
    from src.application.opening_candidate_snapshot import _snapshot_decisions
    from test_opening_candidate_snapshot import _candidate

    strategy = replace(_strategy(), config=replace(_strategy().config, min_annualized_return=.01))
    row = attach_strategy_identity(_candidate(contract_symbol="NVDA-P", period_return=.005), strategy)
    records, _ = _snapshot_decisions(
        {"put": [row]}, statuses=[{"symbol": "NVDA", "strategy_mode": "put"}],
        account="lx", market="US", futu_account_id="fixture", risk_policy_hash="a" * 64, candidate_evaluations=None,
    )
    record = records[0]
    assert record["opening_decision"]["accepted"]
    assert record["normalized_input"]["strategy_core"] == row["strategy_core"]
    assert record["normalized_input_hash"] == canonical_sha256(record["normalized_input"])
    assert record["normalized_input_hash"] != canonical_sha256(_without_identity(record["normalized_input"]))


def test_legacy_snapshot_read_views_do_not_rewrite_bytes_or_hash(tmp_path):
    from src.application.opening_candidate_snapshot import ranked_opening_candidates, ranked_opening_candidate_decisions
    from test_opening_candidate_snapshot import _seal, _snapshot_path

    payload = _seal(tmp_path)
    before = deepcopy(payload)
    path = _snapshot_path(tmp_path)
    original_bytes = path.read_bytes()
    for row in ranked_opening_candidates(payload):
        assert row["strategy_core_status"] == "legacy"
    for row in ranked_opening_candidate_decisions(payload):
        assert row["strategy_core_status"] == "legacy"
    assert payload == before
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("mode", ["put", "call"])
def test_empty_underwriting_preserves_legacy_return_but_checks_injected_scope(mode):
    adapter = enrich_and_filter_sell_put_underwriting if mode == "put" else enrich_and_filter_covered_call_underwriting
    cfgkey = "sell_put_cfg" if mode == "put" else "sell_call_cfg"
    empty = pd.DataFrame()
    kwargs = dict(df_labeled=empty, symbol="NVDA", portfolio_ctx={"filters": {"account": "lx"}},
                  exchange_rate_converter=CONVERTER, **{cfgkey: {"min_dte": "invalid"}})
    assert adapter(**kwargs) is empty
    with pytest.raises(ValueError, match="account"):
        adapter(**kwargs, resolved_strategy=replace(_strategy(mode), account="sy"))


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("value,source,disabled,accepted,reason", [
    (.40, "canonical", False, True, None),
    (.4001, "canonical", False, False, "risk_delta"),
    (0.0, "canonical", False, True, None),
    (None, "canonical", False, False, "risk_delta_unavailable"),
    (.30, "alias", False, False, "risk_delta_unavailable"),
    (.4001, "alias", True, True, None),
])
def test_provider_normalization_to_scan_underwriting_and_sealed_delta(
    frozen_scan, mode, value, source, disabled, accepted, reason,
):
    from src.application.opening_quote_evidence import normalize_option_observation, normalize_underlier_observation
    from src.application.opening_candidate_snapshot import load_opening_candidate_snapshot
    from test_opening_quote_evidence import _chain, _snapshot
    from test_opening_candidate_snapshot import _seal_snapshot, _scan_status, _snapshot_path

    raw = _put_quote("NVDA", 95, 3.0) if mode == "put" else _quote(strike=110)
    signed_delta = None if value is None else (-value if mode == "put" else value)
    provider = _snapshot(code=raw["contract_symbol"], bid_price=3.0, ask_price=3.1,
                         option_delta=signed_delta, option_implied_volatility=30.0,
                         update_time="2026-10-16 10:59:00")
    if source == "alias":
        provider.pop("option_delta")
        provider["delta"] = signed_delta
    underlier = normalize_underlier_observation(
        code="US.NVDA", market="US", now_utc=NOW,
        snapshot_row={"code": "US.NVDA", "last_price": 100.0,
                      "update_time": "2026-10-16 10:59:00", "sec_status": "NORMAL", "suspension": False},
        market_state_row={"code": "US.NVDA", "market_state": "MORNING"},
    )
    observed = normalize_option_observation(
        expected_owner="US.NVDA", market="US", currency="USD",
        chain_row=_chain(code=raw["contract_symbol"], option_type=mode.upper()),
        snapshot_row=provider, underlier_observation=underlier, now_utc=NOW,
        snapshot_requested_at_utc=NOW.isoformat(), snapshot_received_at_utc=NOW.isoformat(),
    )
    assert observed.status == "ready"
    # The runtime frame carries the actual normalizer output, without substituting Delta facts.
    frame = pd.DataFrame([{**raw, **observed.to_dict()}])
    cfg = {"strategy": "insurance_underwriting", **({"max_abs_delta": None} if disabled else {})}
    finals, decisions = [], []
    common = dict(symbol="NVDA", symbol_cfg={}, required_data_dir=frozen_scan,
                  required_data_frame=frame, exchange_rate_converter=CONVERTER,
                  final_candidates_sink_fn=lambda mode, rows: finals.extend(rows),
                  candidate_decisions_sink_fn=lambda mode, rows: decisions.extend(rows))
    if mode == "put":
        run_sell_put_scan_and_summarize(**common, sp=cfg, portfolio_ctx=_portfolio())
    else:
        run_sell_call_scan_and_summarize(
            **common, cc=cfg, stock={"shares": 100, "can_sell_qty": 100, "avg_cost": 80},
            portfolio_ctx={"filters": {"account": "lx"}, "portfolio_source_name": "futu",
                           "capacity_authority": {"status": "available"}},
            locked_shares_status="available", locked_shares_by_symbol={}, locked_shares_unavailable_by_symbol={},
        )
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision["opening_decision"]["accepted"] is accepted
    assert len(finals) == int(accepted)
    facts = decision["normalized_input"]
    assert facts["delta"] == signed_delta
    assert facts["delta_source"] == observed.delta_source
    assert facts["policy_max_abs_delta"] == (None if disabled else .4)
    if reason:
        rejects = decision["opening_decision"]["rejects"]
        assert [item["reason"] for item in rejects] == [reason]
        assert rejects[0]["threshold"] == .4
    sealed = _seal_snapshot(
        frozen_scan, run_id="delta-chain", scan_statuses=[_scan_status("NVDA", mode, "completed")],
        final_candidates={mode: finals}, candidate_evaluations={mode: decisions},
    )
    before = _snapshot_path(frozen_scan, "delta-chain").read_bytes()
    assert load_opening_candidate_snapshot(base=frozen_scan, run_id="delta-chain", account="lx",
                                          require_current_contract=True) == sealed
    assert _snapshot_path(frozen_scan, "delta-chain").read_bytes() == before
    stored = sealed["candidate_decisions"][0]["normalized_input"]
    assert stored["delta"] == signed_delta
    assert stored["delta_source"] == observed.delta_source
