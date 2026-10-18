"""Offline parity: real sealed bundles, deliberate drift, missing evidence and CLI."""

from copy import deepcopy
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
    "portfolio": {"cash_by_currency": {"USD": 100000}, "_sell_put_fx_status": "fresh"},
    "ledger": {
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
    decision["rejects"] = [
        {
            "stage": "stage0_input_normalization",
            "reason": "input_invalid",
            "metric_value": {"reason_code": detail["rule"], "metric_value": detail.get("metric_value")},
            "threshold": detail.get("threshold"),
        }
    ]
    checks = []
    audit._decision_checks(
        {"candidate_id": "bad-quote", "strategy_mode": "put", "normalized_input": row, "opening_decision": decision},
        {"account": "lx", "market": "us"},
        CTX,
        RATES,
        None,
        checks,
    )
    assert any(c["location"].endswith("/calculation/metric_value") and c["status"] == "matched" for c in checks)
    assert any(
        c["location"].endswith("/calculation/adapter") and c["status"] == "insufficient_evidence" for c in checks
    )


def test_capacity_uses_original_floor_division():
    row = {"currency": "USD", "strike": 0.1, "multiplier": 1}
    contexts = {
        "portfolio": {"cash_by_currency": {"USD": 1.0}, "_sell_put_fx_status": None},
        "ledger": {"cash_secured_total_by_ccy": {}},
    }
    assert audit._quantity(row, "put", contexts, CurrencyConverter(RATES)) == 9


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
