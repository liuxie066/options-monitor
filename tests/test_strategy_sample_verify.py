"""Offline parity: real sealed bundles, deliberate drift, missing evidence and CLI."""

from copy import deepcopy
import json
import hashlib
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
def test_legacy_bundle_keeps_partial_comparisons(tmp_path, monkeypatch, contexts, mode):
    seal(tmp_path, [candidate(mode)], mode)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("offline must not access network"))
    first = verify(tmp_path)
    assert first["status"] == "insufficient_evidence", first
    assert any(c["category"] == "capacity_fx" and c["status"] == "insufficient_evidence" for c in first["checks"])
    assert {c["category"] for c in first["checks"]} >= {"metrics", "filter", "quantity", "ranking"}
    assert first == verify(tmp_path)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "field,value,category", [("net_income", 999, "metrics"), ("max_new_contracts", 999, "quantity")]
)
def test_wrong_sealed_results_are_mismatches(tmp_path, contexts, field, value, category):
    row, decision = candidate("call")
    row[field] = value
    decision["normalized_input"] = row
    seal(tmp_path, [(row, decision)], "call")
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


def test_ranking_mismatch_and_complete_tail(tmp_path, contexts):
    payload = seal(tmp_path, [candidate(symbol=s, strike=k) for s in ("NVDA", "AMD") for k in (90, 91)])
    result = verify(tmp_path)
    assert any(c["location"].endswith("/order") and c["status"] == "matched" for c in result["checks"])
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


@pytest.mark.parametrize("changes, expected", [
    ({"bid": -1}, "input_invalid"),
    ({"opening_contract_status": "ineligible"}, "contract_ineligible"),
    ({"opening_contract_status": "market_closed"}, "evidence_unavailable"),
    ({"bid": 0.01, "ask": 0.02, "mid": 0.015}, "policy_rejected"),
])
def test_real_calculation_rejection_replay(tmp_path, changes, expected):
    import pandas as pd
    from test_scan_decision_clock import scan
    strategy = resolve_strategy(mode="put", account="lx", market="us", config=InsuranceUnderwritingConfig())
    decisions = []
    scan("put", tmp_path, pd.DataFrame([quote(**changes)]), decisions,
         quote_freshness_now_utc=NOW, resolved_strategy=strategy)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision["opening_decision"]["rejects"][0]["reason"] == expected
    checks = []
    audit._decision_checks({**decision, "candidate_id": "reject", "strategy_mode": "put"},
                          {"account": "lx", "market": "us"}, CTX, RATES, None, checks)
    assert all(c["status"] == "matched" for c in checks), checks
    decision["opening_decision"]["rejects"][0]["reason"] = "wrong"
    checks = []
    audit._decision_checks({**decision, "candidate_id": "reject", "strategy_mode": "put"},
                          {"account": "lx", "market": "us"}, CTX, RATES, None, checks)
    assert any(c["status"] == "mismatched" for c in checks)


def test_capacity_uses_original_floor_division():
    from tests.cash_evidence_helpers import cash_portfolio
    row = {"symbol": "NVDA", "currency": "USD", "strike": 0.1, "multiplier": 1}
    contexts = {
        "portfolio": cash_portfolio({"cash_by_currency": {"USD": 1.0}}),
        "ledger": {"cash_secured_total_by_ccy": {}, "cash_secured_by_symbol_by_ccy": {},
                   "cash_secured_unavailable_by_symbol": {}, "decision_snapshot_status": "trusted"},
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


def _produce_pipeline_sample(tmp_path, monkeypatch, *, stale=False):
    """Actual prepared workers/loaders and default pipeline; only source facts are synthetic."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    import pandas as pd
    from src.application import (
        candidate_scanning, current_fx_run, futu_portfolio_context, pipeline_context, pipeline_watchlist,
        portfolio_context_service, prepared_option_positions_context, prepared_portfolio_context,
        scan_sell_call, scan_sell_put,
    )
    from src.application.futu_portfolio_context import build_futu_portfolio_context, build_futu_position_snapshot
    from src.application.tick_run_workspace import publish_account_run_config
    from src.infrastructure import exchange_rates
    from tests.cash_evidence_helpers import cash_config
    from test_pipeline_capture_status_routing import _symbol_config

    clock = SimpleNamespace(now=NOW)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now.astimezone(tz or timezone.utc)

    for module in (futu_portfolio_context, pipeline_context, portfolio_context_service, prepared_portfolio_context,
                   prepared_option_positions_context, scan_sell_call, scan_sell_put):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(exchange_rates, "_utc_now", lambda: clock.now)
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("synthetic pipeline must not access network"))
    def frozen_earnings(rows, **kwargs):
        from test_opening_candidate_snapshot import _candidate
        facts = {k: v for k, v in _candidate(contract_symbol="synthetic", period_return=.03).items() if k.startswith("earnings_")}
        result = rows.copy()
        for key, value in facts.items():
            result[key] = pd.Series([deepcopy(value) for _ in range(len(rows))], index=rows.index, dtype=object)
        return result
    monkeypatch.setattr(candidate_scanning, "annotate_candidates_with_earnings_evidence", frozen_earnings)
    env_file = tmp_path / "isolated.env"
    env_file.write_text("")
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("OM_DATA_CONFIG", "")
    data = tmp_path / "portfolio.runtime.json"
    data.write_text("{}\n")
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}\n")
    cfg = cash_config()
    cfg["portfolio"].update(account="lx", broker="富途", data_config=str(data))
    cfg["runtime"] = {"symbol_workers": 1}
    cfg["symbols"] = [_symbol_config(s, market="US", sell_call_enabled=True, combo_enabled=False)
                      for s in ("NVDA", "AMD")]
    for item in cfg["symbols"]:
        for mode in ("sell_put", "sell_call"):
            item[mode]["strategy"] = "insurance_underwriting"
    authority = publish_account_run_config(base=tmp_path, run_id="parity", account="lx", config=cfg)
    cfg = json.loads(authority.canonical_bytes)
    account_state = authority.state_path.parent
    run_state = tmp_path / "output_runs/parity/state"
    fx_snapshot = {"schema_version": 2, "pairs": {
        pair: {"rate": rate, "source": "tencent_quote", "quote_at_utc": NOW.isoformat(),
               "observed_at_utc": NOW.isoformat()}
        for pair, rate in (("USDCNY", 1 / RATES.usd_per_cny), ("HKDCNY", RATES.cny_per_hkd))}}
    monkeypatch.setattr(current_fx_run, "current_exchange_rate_snapshot", lambda **kw: deepcopy(fx_snapshot))
    _, fx_hash = current_fx_run.seal_run_fx_snapshot(base=tmp_path, run_id="parity")

    def broker_facts(**kwargs):
        rows = [{"code": "US." + s, "qty": 300, "can_sell_qty": 300,
                 "average_cost": 80, "currency": "USD", "position_side": "LONG"} for s in ("NVDA", "AMD")]
        positions = build_futu_position_snapshot(
            rows=rows, broker_account_ref={"broker_id": "futu", "external_account_id": "123",
                "environment": "REAL", "account_label": "lx", "broker_account_id": "futu:REAL:123"},
            markets=["US"], asset_types=["stock", "option"], observed_at_utc=NOW.isoformat(), completeness="complete")
        return build_futu_portfolio_context(
            balance_rows=[{"us_cash": 100000}], position_rows=rows, account="lx",
            source_observed_at=NOW.isoformat(), cash_source_observed_at=NOW.isoformat(),
            broker_account_identifiers={"123"}, futu_account_id="123", trd_env="REAL", capacity_market="us",
            exchange_rate_observation=kwargs.get("exchange_rate_observation"), position_snapshot_input=positions)

    monkeypatch.setattr(prepared_portfolio_context, "fetch_futu_portfolio_context", broker_facts)

    class RealWorker:
        returncode = 0

        def __init__(self, command, **kwargs):
            request = Path(command[-1])
            assert prepared_portfolio_context.run_worker(request) == 0

        def poll(self):
            return 0

    portfolio = prepared_portfolio_context.prepare_portfolio_contexts(
        base=tmp_path, repo_root=tmp_path, run_id="parity", account_config_authorities={"lx": authority},
        account_state_dirs={"lx": account_state}, shared_state_dir=run_state, timeout_sec=30,
        popen_factory=RealWorker, fx_snapshot_sha256=fx_hash)["lx"]
    assert portfolio["status"] == "ready", portfolio
    ledger = prepared_option_positions_context.prepare_option_positions_contexts(
        base=tmp_path, run_id="parity", config_path=config_path, account_configs={"lx": cfg},
        account_config_authorities={"lx": authority}, run_state_dir=run_state,
        fx_snapshot_sha256=fx_hash).manifests["lx"]
    assert ledger["status"] == "ready", ledger
    if stale:
        clock.now += timedelta(days=5)
    rows_by_symbol = {}
    for symbol in ("NVDA", "AMD"):
        rows = []
        for mode in ("put", "call"):
            for strike in (91, 90):
                rows.append(quote(mode, symbol=symbol, stock_owner="US." + symbol, strike=strike,
                    contract_symbol=f"US.{symbol}260918{mode[0].upper()}{strike}000", spot=85 if mode == "call" else 100,
                    snapshot_received_at_utc=clock.now.isoformat(), spot_observed_at_utc=clock.now.isoformat()))
        rows_by_symbol[symbol] = pd.DataFrame(rows).to_csv(index=False).encode()
    # Frozen source boundary only. Actual scanner, converter, all internal sinks and seal stay real.
    required_dir = tmp_path / "output_runs/parity/required_data"
    required_dir.mkdir(parents=True)
    required_manifest = run_state / "required_data_snapshot_manifest.json"
    required_manifest.write_text(json.dumps({"synthetic_csv_sha256": {
        symbol: hashlib.sha256(raw).hexdigest() for symbol, raw in rows_by_symbol.items()}}))

    class FrozenBatch:
        def resolve(self, symbol):
            return {"snapshot_id": "quote-" + symbol, "receipt_relpath": f"quotes/{symbol}/receipt.json",
                    "source_outcome": "success_rows", "reason_code": None}, rows_by_symbol[symbol]

    monkeypatch.setattr(pipeline_watchlist, "resolve_frozen_required_data_csv_bytes_batch", lambda **kw: FrozenBatch())
    logs = []
    pipeline_watchlist.run_watchlist_pipeline_default(
        py=sys.executable, base=tmp_path, cfg=cfg, report_dir=account_state.parent, state_dir=account_state,
        shared_state_dir=run_state, required_data_dir=required_dir, is_scheduled=True, top_n=5,
        symbol_timeout_sec=30, portfolio_timeout_sec=30, want_scan=True, no_context=False, symbols_arg=None,
        log=logs.append, want_fn=lambda name: name == "scan", source_account_run_id="parity",
        required_data_snapshot_manifest=required_manifest,
        prepared_portfolio_context_manifest=Path(portfolio["manifest_path"]),
        prepared_portfolio_context_manifest_sha256=portfolio["manifest_sha256"],
        prepared_option_positions_context_manifest=Path(ledger["manifest_path"]),
        prepared_option_positions_context_manifest_sha256=ledger["manifest_sha256"],
        account_config_sha256=authority.account_config_sha256)
    payload = json.loads(_snapshot_path(tmp_path, "parity", "lx").read_text())
    assert payload["replay_evidence"]["capacity_fx"]["source_sha256"] == fx_hash, logs
    assert payload["replay_evidence"]["capacity_fx"]["evaluated_at_utc"] == clock.now.isoformat()
    return payload, clock, logs


def test_full_prepared_pipeline_to_offline_replay(tmp_path, monkeypatch, capsys, record_property):
    payload, clock, logs = _produce_pipeline_sample(tmp_path, monkeypatch)
    assert payload["replay_evidence"]["capacity_fx"]["put_fx_status"] is None
    assert len(payload["ranked_candidates"]) == 8, logs
    report = verify(tmp_path)
    assert report["status"] == "matched", report
    for mode, ids in payload["replay_evidence"]["ranking_input_ids"].items():
        by_id = {d["candidate_id"]: d for d in payload["candidate_decisions"]}
        assert [by_id[cid]["normalized_input"]["symbol"] for cid in ids] == ["NVDA", "NVDA", "AMD", "AMD"]
        assert any(c["location"] == f"ranked_candidates/{mode}/order" and c["status"] == "matched" for c in report["checks"])
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    from datetime import timedelta
    clock.now += timedelta(days=500)
    assert verify(tmp_path) == report
    args = ["--runtime-root", str(tmp_path), "--run-id", "parity", "--account", "lx"]
    assert audit.main(args) == audit.main(args) == 0
    capsys.readouterr()
    cli = subprocess.run([sys.executable, "-B", "-m", "src.application.strategy_sample_verify", *args],
                         capture_output=True, text=True, check=False)
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout) == report
    record_property("offline_cli_exit_code", cli.returncode)
    record_property("snapshot_sha256", report["snapshot_sha256"])
    record_property("manifest_sha256", report["manifest_sha256"])
    record_property("unchanged_input_inventory_sha256", canonical_sha256({str(p.relative_to(tmp_path)): h for p, h in before.items()}))
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("stale", [False, True])
def test_final_fx_override_is_derived_not_trusted(tmp_path, monkeypatch, stale):
    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch, stale=stale)
    fx = payload["replay_evidence"]["capacity_fx"]
    assert fx["status"] == ("unavailable_stale" if stale else "ready")
    assert fx["put_fx_status"] == ("unavailable_stale" if stale else None)
    baseline = verify(tmp_path)
    fx_checks = [c for c in baseline["checks"] if c["category"] == "capacity_fx"]
    assert fx_checks and all(c["status"] == "matched" for c in fx_checks), baseline
    if stale:
        assert fx["usd_per_cny"] is None and fx["cny_per_hkd"] is None
        assert baseline["status"] == "insufficient_evidence"  # native checks remain; no invented CNY metrics
    fx["put_fx_status"] = None if stale else "unavailable_stale"
    write_changed(tmp_path, payload)
    report = verify(tmp_path)
    assert report["status"] == "mismatched", report
    assert any(c["location"].endswith("/put_fx_status") and c["status"] == "mismatched" for c in report["checks"])


@pytest.mark.parametrize("defect", ["source_hash", "missing_override", "bad_time", "duplicate_id", "missing_id", "cross_mode", "bool_version"])
def test_new_evidence_invalid_before_recompute(tmp_path, monkeypatch, defect):
    from src.application.opening_candidate_snapshot import OpeningCandidateSnapshotError, validate_opening_candidate_snapshot
    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch)
    evidence = payload["replay_evidence"]
    if defect == "source_hash":
        evidence["capacity_fx"]["source_snapshot"]["pairs"]["USDCNY"]["rate"] = 99
    elif defect == "missing_override":
        evidence["capacity_fx"].pop("put_fx_status")
    elif defect == "bad_time":
        evidence["capacity_fx"]["evaluated_at_utc"] = "2026-08-06T09:30:00"
    elif defect == "duplicate_id":
        evidence["ranking_input_ids"]["put"].append(evidence["ranking_input_ids"]["put"][0])
    elif defect == "missing_id":
        evidence["ranking_input_ids"]["put"].pop()
    elif defect == "cross_mode":
        evidence["ranking_input_ids"]["put"][0] = evidence["ranking_input_ids"]["call"][0]
    else:
        evidence["schema_version"] = True
    payload["content_sha256"] = canonical_sha256({k: v for k, v in payload.items() if k != "content_sha256"})
    with pytest.raises(OpeningCandidateSnapshotError):
        validate_opening_candidate_snapshot(payload, expected_run_id="parity", expected_account="lx")


def test_legacy_evidence_absence_remains_insufficient(tmp_path, monkeypatch):
    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch)
    payload.pop("replay_evidence")
    write_changed(tmp_path, payload)
    report = verify(tmp_path)
    assert report["status"] == "insufficient_evidence"
    assert any("consumption evidence missing" in c.get("reason", "") for c in report["checks"])
    assert any("input order not archived" in c.get("reason", "") for c in report["checks"])


def test_multisymbol_tail_mismatch_from_real_pipeline(tmp_path, monkeypatch):
    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch)
    rows = [r for r in payload["ranked_candidates"] if r["strategy_mode"] == "put"]
    rows[-1]["rank"], rows[-2]["rank"] = rows[-2]["rank"], rows[-1]["rank"]
    payload["ranked_candidates"] = sorted(payload["ranked_candidates"], key=lambda r: (r["strategy_mode"], r["rank"]))
    write_changed(tmp_path, payload)
    report = verify(tmp_path)
    assert report["status"] == "mismatched", report
    assert any(c["location"] == "ranked_candidates/put/order" and c["status"] == "mismatched" for c in report["checks"])


def test_capacity_adapter_qualifies_and_normalizes_bound_inputs():
    from tests.cash_evidence_helpers import cash_portfolio
    row = {"symbol": "NVDA", "currency": "USD", "strike": 100, "multiplier": 100}
    ctx = {"portfolio": cash_portfolio({"cash_by_currency": {"USD": 30000}}),
           "ledger": {"context_status": "available", "decision_snapshot_status": "trusted",
                      "cash_secured_unavailable_by_symbol": {}, "cash_secured_by_symbol_by_ccy": {},
                      "cash_secured_by_symbol": {"NVDA": 10000}}}
    assert audit._quantity(row, "put", ctx, CurrencyConverter(RATES)) == 2
    ctx["ledger"]["decision_snapshot_status"] = "untrusted"
    with pytest.raises(ValueError, match="option_positions_cash_secured_context_unavailable"):
        audit._quantity(row, "put", ctx, CurrencyConverter(RATES))
    ctx["ledger"]["decision_snapshot_status"] = "trusted"
    ctx["portfolio"]["cash_snapshot"]["status"] = "unknown"
    with pytest.raises(ValueError, match="broker_cash_snapshot_unavailable"):
        audit._quantity(row, "put", ctx, CurrencyConverter(RATES))


def test_recomputed_fx_and_both_prepared_bindings_are_checked(tmp_path, monkeypatch):
    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch)
    contexts, errors = audit._contexts(tmp_path, payload)
    assert not errors
    for kind in ("portfolio", "ledger"):
        changed = deepcopy(contexts)
        if kind == "portfolio":
            changed[kind]["fx_snapshot_sha256"] = "e" * 64
        else:
            changed[kind]["prepared_authority"]["run_fx_snapshot_sha256"] = "e" * 64
        with pytest.raises(ValueError, match="not bound"):
            audit._rates(payload, changed, [])
    payload["replay_evidence"]["capacity_fx"]["usd_per_cny"] = .99
    write_changed(tmp_path, payload)
    report = verify(tmp_path)
    assert report["status"] == "mismatched"
    assert any(c["location"].endswith("/usd_per_cny") and c["status"] == "mismatched" for c in report["checks"])
    # A wrong recording must not become the input used to compute metric/capacity parity.
    assert all(c["status"] == "matched" for c in report["checks"] if c["category"] in {"metrics", "quantity"})
