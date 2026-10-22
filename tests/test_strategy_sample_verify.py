"""Offline parity: real sealed bundles, deliberate drift, missing evidence and CLI."""

from copy import deepcopy
from cash_evidence_helpers import cash_portfolio
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import build_candidate_decision
from om_strategy_core import (
    CallCapacityInputs,
    CurrencyConverter,
    ExchangeRates,
    InsuranceUnderwritingConfig,
    PutCapacityInputs,
    ShortVolPortfolioContext,
    calculate_capacity,
    calculate_constraints,
    calculate_metrics,
    evaluate_candidates,
    resolve_strategy,
)
from src.application import strategy_sample_verify as audit
from src.application.candidate_scanning import attach_strategy_identity
from test_opening_candidate_snapshot import _publish_opening_manifest, _scan_status, _seal_snapshot, _snapshot_path
from test_scan_decision_clock import NOW, quote

RATES = ExchangeRates(0.14, 0.92)
CTX = {
    "portfolio": cash_portfolio({"cash_by_currency": {"USD": 100000}, "_sell_put_fx_status": "fresh", "cash_source_observed_at": NOW.isoformat(), "fx_snapshot_sha256": "d" * 64}, evaluated_at=NOW),
    "ledger": {
        "decision_snapshot_status": "trusted",
        "cash_secured_by_symbol_by_ccy": {},
        "cash_secured_total_by_ccy": {},
        "prepared_authority": {"run_fx_snapshot_sha256": "d" * 64},
        "exchange_rates": {"rates": {"USDCNY": 1 / 0.14, "HKDCNY": 0.92}},
    },
}


def candidate(mode="put", symbol="NVDA", strike=90):
    row = quote(
        mode,
        symbol=symbol,
        stock_owner="US." + symbol,
        strike=strike,
        contract_symbol=f"US.{symbol}260918{mode[0].upper()}{strike}000",
        decision_now_utc=NOW.isoformat(),
        avg_cost=80,
        shares_total=300,
        shares_can_sell=200,
        shares_locked=100,
    )
    # CC strike must meet spot/cost floor.
    if mode == "call":
        row["spot"] = 85
    strategy = resolve_strategy(
        mode=mode, account="lx", market="us", config=InsuranceUnderwritingConfig(min_dte=7, max_dte=60)
    )
    row.pop("net_income_cny", None)
    row.pop("option_contract_point_value_cny", None)
    row.update(calculate_metrics(row, mode=mode, avg_cost=80, exchange_rates=RATES))
    inputs = PutCapacityInputs((("USD", 100000),), (), "fresh") if mode == "put" else CallCapacityInputs(300, 200, 100)
    capacity = calculate_capacity(
        row, mode=mode, capacity_inputs=inputs, exchange_rate_converter=CurrencyConverter(RATES)
    )
    row.update(capacity)
    if mode == "call":
        row["max_new_contracts"] = capacity["covered_contracts_available"]
    row = calculate_constraints(
        row,
        mode=mode,
        risk_ctx=ShortVolPortfolioContext(None, {}, {}, None),
        exchange_rate_converter=CurrencyConverter(RATES),
        strategy=strategy,
    )
    row = attach_strategy_identity(row, strategy)
    decision = evaluate_candidates([row], strategy=strategy)["decisions"][0]["opening_decision"]
    assert decision["accepted"], decision["rejects"]
    return decision["normalized_input"], decision


def seal(base, records, mode="put"):
    rows = [r for r, d in records if d["accepted"]]
    payload = _seal_snapshot(
        base,
        run_id="parity",
        account="lx",
        scan_statuses=[_scan_status(s, mode, "completed") for s in sorted({r["symbol"] for r, d in records})],
        final_candidates={mode: rows},
        candidate_evaluations={mode: [{"normalized_input": r, "opening_decision": d} for r, d in records]},
    )
    _publish_opening_manifest(base, payload)
    return payload


def write_changed(base, payload):
    payload["content_sha256"] = canonical_sha256({k: v for k, v in payload.items() if k != "content_sha256"})
    _snapshot_path(base, "parity", "lx").write_text(json.dumps(payload))
    # Publish through the owner after updating only the offline fixture snapshot.
    manifest = base / "output_runs/parity/accounts/lx/state/candidate_snapshot_manifest.v4.json"
    manifest.unlink()
    _publish_opening_manifest(base, payload)


@pytest.fixture
def contexts(monkeypatch):
    monkeypatch.setattr(audit, "_contexts", lambda *_: (deepcopy(CTX), {}))


def verify(base):
    return audit.verify_sample(base=base, run_id="parity", account="lx")


@pytest.mark.parametrize("mode", ["put", "call"])
def test_stage_parity_on_real_sealed_bundle(tmp_path, monkeypatch, contexts, mode):
    seal(tmp_path, [candidate(mode)], mode)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("offline must not access network"))
    first = verify(tmp_path)
    assert first["status"] == "matched", first
    assert {c["category"] for c in first["checks"]} >= {"metrics", "filter", "quantity", "ranking"}
    assert first == verify(tmp_path)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "field,value,category", [("net_income", 999, "metrics"), ("max_new_contracts", 999, "quantity")]
)
def test_wrong_sealed_results_are_mismatches(tmp_path, contexts, field, value, category):
    row, decision = candidate()
    row[field] = value
    decision["normalized_input"] = row
    seal(tmp_path, [(row, decision)])
    result = verify(tmp_path)
    assert result["status"] == "mismatched"
    assert any(c["category"] == category and c["status"] == "mismatched" for c in result["checks"])


def test_changed_filter_is_not_replayed_as_truth(tmp_path, contexts):
    row, decision = candidate()
    decision = build_candidate_decision(
        mode="put",
        symbol=row["symbol"],
        contract_symbol=row["contract_symbol"],
        accepted=False,
        rejects=[{"stage": "stage2_return_floor", "reason": "return_annualized", "message": "wrong"}],
        normalized_input=row,
    )
    seal(tmp_path, [(row, decision)])
    result = verify(tmp_path)
    assert result["status"] == "mismatched"
    assert any(c["location"].endswith("/accepted") and c["status"] == "mismatched" for c in result["checks"])


@pytest.mark.parametrize("missing", ["decision_now_utc", "strategy_core", "net_income", "max_new_contracts"])
def test_missing_evidence_not_defaulted(tmp_path, contexts, missing):
    row, decision = candidate()
    row.pop(missing)
    decision["normalized_input"] = row
    seal(tmp_path, [(row, decision)])
    result = verify(tmp_path)
    assert result["status"] == "insufficient_evidence", result
    assert any(c["status"] == "insufficient_evidence" for c in result["checks"])


def test_unsupported_core_version_does_not_execute(tmp_path, monkeypatch, contexts):
    row, decision = candidate()
    row["strategy_core"]["core_version"] = "99.0.0"
    row["strategy_core"]["runtime"]["core_version"] = "99.0.0"
    decision["normalized_input"] = row
    monkeypatch.setattr(audit, "calculate_metrics", lambda *a, **k: pytest.fail("unknown code must not execute"))
    checks = []
    eligible = audit._decision_checks(
        {"candidate_id": "unknown", "strategy_mode": "put", "normalized_input": row, "opening_decision": decision},
        {"account": "lx", "market": "us"},
        CTX,
        RATES,
        None,
        checks,
    )
    assert not eligible
    assert checks[0]["status"] == "insufficient_evidence"


def test_prepared_context_gap_keeps_native_comparisons(tmp_path):
    seal(tmp_path, [candidate()])
    result = verify(tmp_path)
    assert result["status"] == "insufficient_evidence"
    assert result["dependency_gaps"]
    assert any(c["category"] == "metrics" and c["status"] == "matched" for c in result["checks"])
    assert any(c["category"] == "quantity" and c["status"] == "insufficient_evidence" for c in result["checks"])


def test_missing_dependency_and_corrupt_snapshot_block_execution(tmp_path, monkeypatch):
    seal(tmp_path, [candidate()])
    p = tmp_path / "output_runs/parity/state/required_data_snapshot_manifest.json"
    p.write_text("broken")
    monkeypatch.setattr(audit, "calculate_metrics", lambda *a, **k: pytest.fail("invalid bundle"))
    report = verify(tmp_path)
    assert report["status"] == "insufficient_evidence"
    assert [c["category"] for c in report["checks"]] == ["integrity"]


@pytest.mark.parametrize(
    "account,run", [("sy", "parity"), ("lx", "other"), ("../lx", "parity"), ("lx", "../parity"), ("LX", "parity")]
)
def test_explicit_scope_and_path_rejection(tmp_path, account, run):
    seal(tmp_path, [candidate()])
    result = audit.verify_sample(base=tmp_path, run_id=run, account=account)
    assert result["status"] == "insufficient_evidence"


def test_ranking_mismatch_and_ambiguous_tail(tmp_path, contexts):
    payload = seal(tmp_path, [candidate(symbol=s, strike=k) for s in ("NVDA", "AMD") for k in (90, 91)])
    payload.pop("ranking_inputs", None)
    write_changed(tmp_path, payload)
    result = verify(tmp_path)
    assert any(c.get("reason") == "original symbol-group input order not archived" for c in result["checks"])
    payload["ranked_candidates"][0], payload["ranked_candidates"][1] = (
        payload["ranked_candidates"][1],
        payload["ranked_candidates"][0],
    )
    for i, r in enumerate(payload["ranked_candidates"], 1):
        r["rank"] = i
    write_changed(tmp_path, payload)
    result = verify(tmp_path)
    assert any(c["category"] == "ranking" and c["status"] == "mismatched" for c in result["checks"])


def test_projection_fact_drift(tmp_path, contexts):
    payload = seal(tmp_path, [candidate()])
    payload["ranked_candidates"][0]["facts"]["max_new_contracts"] = 999
    write_changed(tmp_path, payload)
    result = verify(tmp_path)
    assert any(c["category"] == "ranking_facts" and c["status"] == "mismatched" for c in result["checks"])


def test_cli_json_gap_exit_code(tmp_path):
    seal(tmp_path, [candidate()])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.application.strategy_sample_verify",
            "--runtime-root",
            str(tmp_path),
            "--run-id",
            "parity",
            "--account",
            "lx",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["status"] == "insufficient_evidence"


def test_no_vacuous_pass(tmp_path):
    payload = _seal_snapshot(
        tmp_path,
        run_id="parity",
        scan_statuses=[_scan_status("NVDA", "put", "unavailable", reason="test_empty")],
        final_candidates={"put": []},
    )
    _publish_opening_manifest(tmp_path, payload)
    assert verify(tmp_path)["status"] == "insufficient_evidence"


def test_strict_value_comparison():
    assert not audit._equal(True, 1)
    assert not audit._equal(float("nan"), float("nan"))
    assert not audit._equal({"v": None}, {})
    assert not audit._equal([1, 2], [2, 1])


@pytest.mark.parametrize("field", ["shares_total", "shares_can_sell", "shares_locked"])
def test_cc_missing_share_fact(tmp_path, contexts, field):
    row, decision = candidate("call")
    row.pop(field)
    decision["normalized_input"] = row
    seal(tmp_path, [(row, decision)], "call")
    result = verify(tmp_path)
    assert result["status"] == "insufficient_evidence"
    assert any(c["category"] == "quantity" and c["status"] == "insufficient_evidence" for c in result["checks"])


@pytest.mark.parametrize("field", ["cash_by_currency", "_sell_put_fx_status"])
def test_csp_missing_raw_fact(tmp_path, monkeypatch, field):
    seal(tmp_path, [candidate()])
    ctx = deepcopy(CTX)
    ctx["portfolio"].pop(field)
    monkeypatch.setattr(audit, "_contexts", lambda *_: (ctx, {}))
    assert verify(tmp_path)["status"] == "insufficient_evidence"


def test_wrong_fx_generation(tmp_path, monkeypatch):
    seal(tmp_path, [candidate()])
    ctx = deepcopy(CTX)
    ctx["ledger"]["prepared_authority"]["run_fx_snapshot_sha256"] = "e" * 64
    monkeypatch.setattr(audit, "_contexts", lambda *_: (ctx, {}))
    result = verify(tmp_path)
    assert result["status"] == "insufficient_evidence"
    assert any("not bound" in c.get("reason", "") for c in result["checks"])


def test_partial_scope_cannot_pass(tmp_path, contexts):
    row, decision = candidate()
    payload = _seal_snapshot(
        tmp_path,
        run_id="parity",
        scan_statuses=[
            _scan_status("NVDA", "put", "completed"),
            _scan_status("AMD", "put", "failed", reason="provider_failed"),
        ],
        final_candidates={"put": [row]},
        candidate_evaluations={"put": [{"normalized_input": row, "opening_decision": decision}]},
    )
    _publish_opening_manifest(tmp_path, payload)
    result = verify(tmp_path)
    assert result["status"] == "insufficient_evidence"
    assert any(c["category"] == "coverage" for c in result["checks"])


@pytest.mark.parametrize("clock", ["invalid", "2026-08-06T00:00:00", None])
def test_bad_clock_never_falls_back(tmp_path, contexts, clock):
    row, decision = candidate()
    row["decision_now_utc"] = clock
    decision["normalized_input"] = row
    checks = []
    assert not audit._decision_checks(
        {"candidate_id": "bad-time", "strategy_mode": "put", "normalized_input": row, "opening_decision": decision},
        {"account": "lx", "market": "us"},
        CTX,
        RATES,
        None,
        checks,
    )
    assert checks[0]["status"] == "insufficient_evidence"


def test_real_prepared_portfolio_loader(tmp_path):
    from test_prepared_portfolio_context import _config_authorities, _prepare, _state_dirs

    shared, states = _state_dirs(tmp_path, "parity")
    authorities = _config_authorities(tmp_path, "parity")
    manifests = _prepare(tmp_path, shared, states, "parity", authorities=authorities)
    manifest = manifests["lx"]
    snapshot = {
        "run_id": "parity",
        "account": "lx",
        "account_config_sha256": authorities["lx"].account_config_sha256,
        "dependencies": [
            {
                "kind": "portfolio",
                "relpath": str(Path(manifest["manifest_path"]).relative_to(tmp_path)),
                "sha256": manifest["manifest_sha256"],
            }
        ],
    }
    contexts, errors = audit._contexts(tmp_path, snapshot)
    assert contexts["portfolio"]["stocks_by_symbol"]["NVDA"]["avg_cost"] == 100
    assert "portfolio" not in errors
    assert "ledger" in errors
    snapshot["account_config_sha256"] = "f" * 64
    contexts, errors = audit._contexts(tmp_path, snapshot)
    assert "portfolio" not in contexts
    assert "portfolio" in errors


def test_cli_mismatch_precedes_partial_evidence(tmp_path):
    row, decision = candidate()
    row["net_income"] = 999
    decision["normalized_input"] = row
    seal(tmp_path, [(row, decision)])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.application.strategy_sample_verify",
            "--runtime-root",
            str(tmp_path),
            "--run-id",
            "parity",
            "--account",
            "lx",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["counts"]["insufficient_evidence"] > 0


def test_calculation_rejection_preserves_partial_boundary():
    from domain.domain.engine import CandidateCalculationError

    row, decision = candidate()
    row["bid"] = -1
    with pytest.raises(CandidateCalculationError) as caught:
        calculate_metrics(row, mode="put", exchange_rates=RATES)
    detail = caught.value.to_payload()
    decision["accepted"] = False
    decision["rejects"] = [audit.calculation_rejection(row, detail)]
    checks = []
    audit._decision_checks(
        {"candidate_id": "bad-quote", "strategy_mode": "put", "normalized_input": row, "opening_decision": decision},
        {"account": "lx", "market": "us"},
        CTX,
        RATES,
        None,
        checks,
    )
    assert any(c["location"].endswith("/calculation/rejects") and c["status"] == "matched" for c in checks)
    assert not any(c["status"] != "matched" for c in checks)


@pytest.mark.parametrize("changes, expected", [
    ({"bid": -1}, "input_invalid"),
    ({"opening_contract_status": "ineligible"}, "contract_ineligible"),
    ({"opening_contract_status": "market_closed"}, "evidence_unavailable"),
    ({"bid": 0.01, "ask": 0.02, "mid": 0.015}, "policy_rejected"),
])
def test_real_calculation_rejection_replay(tmp_path, monkeypatch, changes, expected):
    import pandas as pd
    from src.application import candidate_scanning
    from test_scan_decision_clock import scan

    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("synthetic rejection must not access network"))
    monkeypatch.setattr(candidate_scanning, "annotate_candidates_with_earnings_evidence",
                        lambda *a, **k: pytest.fail("calculation rejection must not request earnings evidence"))
    strategy = resolve_strategy(mode="put", account="lx", market="us", config=InsuranceUnderwritingConfig())
    decisions = []
    candidates = scan("put", tmp_path, pd.DataFrame([quote(**changes)]), decisions,
                      quote_freshness_now_utc=NOW, resolved_strategy=strategy)
    assert candidates.empty
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision["opening_decision"]["rejects"][0]["reason"] == expected
    checks = []
    audit._decision_checks({**decision, "candidate_id": "reject", "strategy_mode": "put"},
                          {"account": "lx", "market": "us"}, CTX, RATES, None, checks)
    assert checks and all(c["status"] == "matched" for c in checks), checks
    decision["opening_decision"]["rejects"][0]["reason"] = "wrong"
    checks = []
    audit._decision_checks({**decision, "candidate_id": "reject", "strategy_mode": "put"},
                          {"account": "lx", "market": "us"}, CTX, RATES, None, checks)
    assert any(c["status"] == "mismatched" for c in checks)


def test_capacity_uses_original_floor_division():
    row = {"symbol": "NVDA", "currency": "USD", "strike": 0.1, "multiplier": 1}
    contexts = deepcopy(CTX)
    contexts["portfolio"]["cash_by_currency"] = {"USD": 1.0}
    contexts["portfolio"]["_sell_put_fx_status"] = None
    assert audit._quantity(row, "put", contexts, CurrencyConverter(RATES)) == (9, None)


def test_real_prepared_ledger_loader(tmp_path, monkeypatch):
    from src.application import prepared_option_positions_context as owner
    from test_prepared_option_positions_context import _prepare_env, _prepare, _fx_stub

    configs, authorities, config_path, _ = _prepare_env(tmp_path, "parity")
    monkeypatch.setattr(owner, "get_exchange_rates_or_fetch_latest", _fx_stub())
    batch = _prepare(tmp_path, run_id="parity", config_path=config_path, configs=configs, authorities=authorities)
    manifest = batch.manifests["lx"]
    snapshot = {
        "run_id": "parity",
        "account": "lx",
        "account_config_sha256": authorities["lx"].account_config_sha256,
        "dependencies": [
            {
                "kind": "ledger",
                "relpath": str(Path(manifest["manifest_path"]).relative_to(tmp_path)),
                "sha256": manifest["manifest_sha256"],
            }
        ],
    }
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("offline must not access network"))
    contexts, errors = audit._contexts(tmp_path, snapshot)
    assert "ledger" in contexts, errors
    assert contexts["ledger"]["cash_secured_total_by_ccy"] == {}
    assert "ledger" not in errors
    snapshot["dependencies"][0]["sha256"] = "f" * 64
    contexts, errors = audit._contexts(tmp_path, snapshot)
    assert "ledger" not in contexts
    assert "ledger" in errors

@pytest.mark.parametrize("status,rule,expected", [
    ("ineligible", "evidence_unavailable", "contract_ineligible"),
    ("market_closed", "option_multiplier_conflict", "evidence_unavailable"),
    ("data_unavailable", "net_premium_non_positive", "evidence_unavailable"),
    ("ready", "evidence_unavailable", "evidence_unavailable"),
    ("ready", "contract_ineligible", "contract_ineligible"),
    ("ready", "option_multiplier_conflict", "contract_ineligible"),
    (None, "net_premium_non_positive", "input_invalid"),
    ("ready", "net_premium_non_positive", "policy_rejected"),
    ("ready", "unknown", "input_invalid"),
])
def test_shared_calculation_rejection_precedence(status, rule, expected):
    reject = audit.calculation_rejection(
        {"opening_contract_status": status},
        {"rule": rule, "message": "detail", "metric_value": -1, "threshold": 0},
    )
    assert reject == {"stage": "stage0_input_normalization", "reason": expected, "message": "detail",
                      "metric_value": {"reason_code": rule, "metric_value": -1}, "threshold": 0}


@pytest.mark.parametrize("field,value", [
    ("stage", "other"), ("reason", "policy_rejected"), ("message", "wrong"),
    ("metric_value", {"reason_code": "wrong", "metric_value": 9}), ("threshold", 999),
])
def test_complete_calculation_reject_drift_detected(field, value):
    from domain.domain.engine import CandidateCalculationError

    row, decision = candidate()
    row["bid"] = -1
    with pytest.raises(CandidateCalculationError) as caught:
        calculate_metrics(row, mode="put", exchange_rates=RATES)
    reject = audit.calculation_rejection(row, caught.value.to_payload())
    reject[field] = value
    decision.update(accepted=False, rejects=[reject])
    checks = []
    audit._decision_checks(
        {"candidate_id": "drift", "strategy_mode": "put", "normalized_input": row, "opening_decision": decision},
        {"account": "lx", "market": "us"}, CTX, RATES, None, checks,
    )
    assert any(c["status"] == "mismatched" for c in checks), checks


def test_calculation_missing_status_remains_insufficient():
    from domain.domain.engine import CandidateCalculationError

    row, decision = candidate()
    row["bid"] = -1
    row.pop("opening_contract_status")
    with pytest.raises(CandidateCalculationError) as caught:
        calculate_metrics(row, mode="put", exchange_rates=RATES)
    decision.update(accepted=False, rejects=[audit.calculation_rejection(row, caught.value.to_payload())])
    checks = []
    audit._decision_checks(
        {"candidate_id": "gap", "strategy_mode": "put", "normalized_input": row, "opening_decision": decision},
        {"account": "lx", "market": "us"}, CTX, RATES, None, checks,
    )
    assert any(c.get("reason") == "sealed opening contract status missing" for c in checks)


def test_blocked_capacity_owner_before_missing_fx_and_quantity_label():
    row, decision = candidate()
    ctx = deepcopy(CTX)
    ctx["portfolio"].pop("_sell_put_fx_status")
    ctx["ledger"]["cash_secured_unavailable_by_symbol"] = {"NVDA": "option_close_settlement_pending"}
    reason = "NVDA:option_close_settlement_pending"
    assert audit._quantity(row, "put", ctx, CurrencyConverter(RATES), fx_error="missing FX") == (0, reason)
    row.update(max_new_contracts=0, cash_fx_status=reason)
    checks = []
    audit._decision_checks(
        {"candidate_id": "blocked", "strategy_mode": "put", "normalized_input": row, "opening_decision": decision},
        {"account": "lx", "market": "us"}, ctx, RATES, "missing FX", checks,
    )
    blocked = [c for c in checks if c["category"] == "capacity_unavailable"]
    assert len(blocked) == 2 and all(c["status"] == "matched" for c in blocked)
    assert not any(c["category"] == "quantity" and c["status"] == "matched" for c in checks)


@pytest.mark.parametrize("missing", ["portfolio", "ledger", "cash_snapshot", "decision_snapshot_status"])
def test_missing_prepared_qualification_is_not_matched_business_reject(missing):
    row, _ = candidate()
    ctx = deepcopy(CTX)
    if missing in ("portfolio", "ledger"):
        ctx.pop(missing)
    else:
        ctx["portfolio" if missing == "cash_snapshot" else "ledger"].pop(missing)
    with pytest.raises(ValueError, match="(unavailable|missing)"):
        audit._quantity(row, "put", ctx, CurrencyConverter(RATES))


def test_new_seal_replays_full_multigroup_tail(tmp_path, contexts):
    records = [candidate(symbol=s, strike=k) for s in ("NVDA", "AMD") for k in (91, 90)]
    payload = seal(tmp_path, records)
    assert payload["ranking_inputs"]["put"]["candidate_ids"]
    result = verify(tmp_path)
    assert result["status"] == "matched", result
    full_order = [c for c in result["checks"] if c["category"] == "ranking" and c["location"].endswith("/order")]
    assert len(full_order) == 1 and full_order[0]["status"] == "matched"
    payload["ranked_candidates"][-2:] = list(reversed(payload["ranked_candidates"][-2:]))
    for i, ranked in enumerate(payload["ranked_candidates"], 1):
        ranked["rank"] = i
    write_changed(tmp_path, payload)
    assert any(c["status"] == "mismatched" and c["location"].endswith("/order") for c in verify(tmp_path)["checks"])


def capacity_fx_evidence(*, marker_present=False, marker_value=None, usd=None, hkd=None):
    from domain.domain.decision_state_fingerprint import canonical_sha256

    observation = {"rates": {}}
    if usd is not None:
        observation["rates"]["USDCNY"] = 1 / usd
    if hkd is not None:
        observation["rates"]["HKDCNY"] = hkd
    return {"schema_version": 1, "evaluated_at_utc": NOW.isoformat(), "observation_status": "ready",
            "observation": observation, "observation_sha256": canonical_sha256(observation),
            "usd_per_cny": usd, "cny_per_hkd": hkd, "run_fx_snapshot_sha256": "d" * 64,
            "marker_present": marker_present, **({"marker_value": marker_value} if marker_present else {})}


def test_replay_rates_consume_actual_projection_instead_of_prepared():
    evidence = capacity_fx_evidence(usd=None, hkd=0.93)
    snapshot = {"capacity_fx_evidence": evidence, "dependencies": [{"kind": "fx", "sha256": "d" * 64}]}
    actual = audit._rates(snapshot, CTX)
    assert actual.usd_per_cny is None and actual.cny_per_hkd == 0.93
    evidence["usd_per_cny"] = 0.14
    with pytest.raises(ValueError, match="scalars disagree"):
        audit._rates(snapshot, CTX)


@pytest.mark.parametrize("present,value", [(False, None), (True, None), (True, "unavailable_stale")])
def test_sealed_fx_marker_overrides_only_from_presence_fact(present, value):
    ctx = deepcopy(CTX)
    ctx["portfolio"].pop("_sell_put_fx_status")
    if present:
        ctx["portfolio"]["_sell_put_fx_status"] = value
    row, _ = candidate()
    quantity, reason = audit._quantity(
        row, "put", ctx, CurrencyConverter(RATES),
        snapshot={"capacity_fx_evidence": capacity_fx_evidence(marker_present=present, marker_value=value)},
    )
    # Native USD funds do not need conversion even with a stale cross-currency observation.
    assert quantity == 11 and reason is None


def test_blocked_capacity_reason_and_label_drift_are_mismatches():
    row, decision = candidate()
    ctx = deepcopy(CTX)
    ctx["ledger"]["cash_secured_unavailable_by_symbol"] = {"NVDA": "option_close_settlement_pending"}
    row.update(max_new_contracts=999, cash_fx_status="wrong")
    checks = []
    audit._decision_checks(
        {"candidate_id": "blocked-drift", "strategy_mode": "put", "normalized_input": row, "opening_decision": decision},
        {"account": "lx", "market": "us"}, ctx, RATES, None, checks,
    )
    assert sum(c["category"] == "capacity_unavailable" and c["status"] == "mismatched" for c in checks) == 2


@pytest.mark.parametrize("change", [
    {}, {"status": "fresh"}, {"status": []}, {"reason_codes": "missing"},
    {"source_observed_at": "bad"}, {"evaluated_at": None}, {"max_age_sec": True},
])
def test_malformed_cash_verdict_remains_evidence_gap(change):
    row, _ = candidate()
    ctx = deepcopy(CTX)
    ctx["portfolio"]["cash_snapshot"] = change if change in ({}, {"status": "fresh"}) else {
        **ctx["portfolio"]["cash_snapshot"], **change,
    }
    with pytest.raises(ValueError, match="qualification verdict missing"):
        audit._quantity(row, "put", ctx, CurrencyConverter(RATES))


def test_explicit_stale_cash_verdict_reproduces_block_before_fx():
    row, _ = candidate()
    ctx = deepcopy(CTX)
    ctx["portfolio"]["cash_snapshot"].update(status="stale", reason_codes=["CASH_OBSERVATION_STALE"])
    ctx["portfolio"].pop("_sell_put_fx_status")
    assert audit._quantity(row, "put", ctx, CurrencyConverter(RATES), fx_error="missing FX") == (
        0, "broker_cash_snapshot_unavailable",
    )


@pytest.mark.parametrize("gap", ["marker", "fx_binding"])
def test_fx_dependent_block_cannot_certify_missing_historical_fx(gap):
    row, _ = candidate()
    ctx = deepcopy(CTX)
    ctx["portfolio"]["cash_by_currency"] = {"CNY": 100000}
    if gap == "marker":
        ctx["portfolio"].pop("_sell_put_fx_status")
    with pytest.raises(ValueError, match="(FX evidence|not bound)"):
        audit._quantity(row, "put", ctx, CurrencyConverter(ExchangeRates()),
                        fx_error="FX not bound" if gap == "fx_binding" else None)


def test_numeric_capacity_rejects_foreign_portfolio_fx_generation():
    row, _ = candidate()
    ctx = deepcopy(CTX)
    ctx["portfolio"]["fx_snapshot_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="portfolio FX facts are not bound"):
        audit._quantity(row, "put", ctx, CurrencyConverter(RATES),
                        snapshot={"dependencies": [{"kind": "fx", "sha256": "d" * 64}]})


@pytest.mark.parametrize("change", [
    {"marker_present": False},
    {"marker_present": True, "marker_value": None},
    {"marker_present": True, "marker_value": "unavailable_stale"},
])
def test_sealed_fx_marker_must_follow_prepared_facts_and_shared_scan_rule(change):
    evidence = capacity_fx_evidence(marker_present=True, marker_value="fresh")
    evidence.update(change)
    if not evidence["marker_present"]:
        evidence.pop("marker_value")
    row, _ = candidate()
    with pytest.raises(ValueError, match="marker disagrees"):
        audit._quantity(row, "put", deepcopy(CTX), CurrencyConverter(RATES),
                        snapshot={"capacity_fx_evidence": evidence})


def test_stale_projection_requires_original_scan_marker_assignment():
    evidence = capacity_fx_evidence(marker_present=True, marker_value="unavailable_stale")
    evidence["observation_status"] = "unavailable_stale"
    row, _ = candidate()
    quantity, reason = audit._quantity(row, "put", deepcopy(CTX), CurrencyConverter(RATES),
                                     snapshot={"capacity_fx_evidence": evidence})
    assert quantity == 11 and reason is None
    for presence, value in ((False, None), (True, None), (True, "fresh")):
        evidence["marker_present"] = presence
        if presence:
            evidence["marker_value"] = value
        else:
            evidence.pop("marker_value", None)
        with pytest.raises(ValueError, match="marker disagrees"):
            audit._quantity(row, "put", deepcopy(CTX), CurrencyConverter(RATES),
                            snapshot={"capacity_fx_evidence": evidence})


def test_old_template_snapshot_remains_readable_but_cannot_execute(tmp_path, contexts):
    from domain.domain.strategy_identity import implementation_policy_hash
    from src.application.opening_candidate_snapshot import load_opening_candidate_snapshot
    payload=seal(tmp_path,[candidate()])
    # Model an already sealed old archive, never ask the new execution owner to create one.
    def old_identity(value):
        if isinstance(value, dict):
            if 'strategy_core' in value:
                identity=value['strategy_core']
                identity['core_version']=identity['template_version']='0.1.0'
                identity['runtime']['core_version']='0.1.0'
                identity['effective_parameters'].pop('max_abs_delta',None)
                identity['effective_policy_hash']=implementation_policy_hash(identity)
            for child in value.values(): old_identity(child)
        elif isinstance(value,list):
            for child in value: old_identity(child)
    old_identity(payload)
    for decision in payload['candidate_decisions']:
        decision['normalized_input_hash']=canonical_sha256(decision['normalized_input'])
        decision['decision_hash']=canonical_sha256(decision['opening_decision'])
        for ranked in [*payload['ranked_candidates'], *payload['scope_results']]:
            if ranked.get('candidate_id')==decision['candidate_id']:
                ranked['decision_hash']=decision['decision_hash']
                if 'normalized_input_hash' in ranked:
                    ranked['normalized_input_hash']=decision['normalized_input_hash']
    write_changed(tmp_path,payload)
    before=_snapshot_path(tmp_path,'parity','lx').read_bytes()
    assert load_opening_candidate_snapshot(base=tmp_path,run_id='parity',account='lx')==payload
    result=verify(tmp_path)
    assert result['status']=='insufficient_evidence'
    assert any('unavailable strategy template' in str(c) for c in result['checks'])
    assert _snapshot_path(tmp_path,'parity','lx').read_bytes()==before
