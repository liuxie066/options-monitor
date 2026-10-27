"""PR851's synthetic pipeline fixture, adapted to the unique current evidence schema.

Actual prepared worker bodies/loaders, default pipeline, scanner, sinks, seal and
verifier remain real. In-process worker execution does not test OS worker lifecycle.
The socket guard covers this process; CLI isolation uses explicit env/root arguments.
"""

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from om_strategy_core import ExchangeRates
from domain.domain.strategy_identity import candidate_ranking_profile
from src.application import strategy_sample_verify as audit
from test_opening_candidate_snapshot import _publish_opening_manifest, _snapshot_path
from test_scan_decision_clock import NOW, quote

RATES = ExchangeRates(0.14, 0.92)


def _verify(base):
    return audit.verify_sample(base=base, run_id="parity", account="lx")


def _inventory(base):
    return {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in base.rglob("*") if p.is_file()}


def _write_changed(base, payload):
    payload["content_sha256"] = canonical_sha256({k: v for k, v in payload.items() if k != "content_sha256"})
    _snapshot_path(base, "parity", "lx").write_text(json.dumps(payload))
    manifest = base / "output_runs/parity/accounts/lx/state/candidate_snapshot_manifest.v4.json"
    manifest.unlink()
    _publish_opening_manifest(base, payload)


def _produce_pipeline_sample(tmp_path, monkeypatch, *, stale=False, prepared_marker=...):
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
    from cash_evidence_helpers import cash_config
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
    from src.application.ledger.queries import resolve_position_ledger_sqlite_path

    assert resolve_position_ledger_sqlite_path(base=tmp_path, data_config=data).is_relative_to(tmp_path)
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
    monkeypatch.setattr(current_fx_run, "current_exchange_rate_snapshot",
                        lambda **kw: pytest.fail("sealed pipeline must not refresh FX source"))

    def broker_facts(**kwargs):
        rows = [{"code": "US." + s, "qty": 300, "can_sell_qty": 300,
                 "average_cost": 80, "currency": "USD", "position_side": "LONG"} for s in ("NVDA", "AMD")]
        positions = build_futu_position_snapshot(
            rows=rows, broker_account_ref={"broker_id": "futu", "external_account_id": "123",
                "environment": "REAL", "account_label": "lx", "broker_account_id": "futu:REAL:123"},
            markets=["US"], asset_types=["stock", "option"], observed_at_utc=NOW.isoformat(), completeness="complete")
        result = build_futu_portfolio_context(
            balance_rows=[{"us_cash": 100000}], position_rows=rows, account="lx",
            source_observed_at=NOW.isoformat(), cash_source_observed_at=NOW.isoformat(),
            broker_account_identifiers={"123"}, futu_account_id="123", trd_env="REAL", capacity_market="us",
            exchange_rate_observation=kwargs.get("exchange_rate_observation"), position_snapshot_input=positions)

        if prepared_marker is not ...:
            result["_sell_put_fx_status"] = prepared_marker
        return result

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
    assert Path(portfolio["manifest_path"]).resolve().is_relative_to(tmp_path)
    ledger = prepared_option_positions_context.prepare_option_positions_contexts(
        base=tmp_path, run_id="parity", config_path=config_path, account_configs={"lx": cfg},
        account_config_authorities={"lx": authority}, run_state_dir=run_state,
        fx_snapshot_sha256=fx_hash).manifests["lx"]
    assert ledger["status"] == "ready", ledger
    assert Path(ledger["manifest_path"]).resolve().is_relative_to(tmp_path)
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
    assert payload["capacity_fx_evidence"]["run_fx_snapshot_sha256"] == fx_hash, logs
    assert payload["capacity_fx_evidence"]["evaluated_at_utc"] == clock.now.isoformat()
    return payload, clock, logs



def test_full_prepared_pipeline_to_offline_replay(tmp_path, monkeypatch, capsys, record_property):
    payload, clock, logs = _produce_pipeline_sample(tmp_path, monkeypatch)
    evidence = payload["capacity_fx_evidence"]
    assert evidence["observation_status"] == "ready"
    assert evidence["marker_present"] is False
    assert "marker_value" not in evidence
    assert len(payload["candidate_decisions"]) == len(payload["ranked_candidates"]) == 8, logs
    assert all(d["opening_decision"]["accepted"] for d in payload["candidate_decisions"])
    report = _verify(tmp_path)
    assert report["status"] == "matched", report
    assert report["counts"]["mismatched"] == report["counts"]["insufficient_evidence"] == 0
    assert set(payload["ranking_inputs"]) == {"put", "call"}
    by_id = {d["candidate_id"]: d for d in payload["candidate_decisions"]}
    for mode, inputs in payload["ranking_inputs"].items():
        rows = [by_id[cid]["normalized_input"] for cid in inputs["candidate_ids"]]
        assert [r["symbol"] for r in rows] == ["NVDA", "NVDA", "AMD", "AMD"]
        assert len(set(inputs["candidate_ids"])) == 4
        assert inputs["profile"] == candidate_ranking_profile(rows, mode=mode)
        check = next(c for c in report["checks"] if c["location"] == f"ranked_candidates/{mode}/order")
        assert check["status"] == "matched"
        assert check["recorded"] == check["recomputed"] == [
            r["facts"]["contract_symbol"] for r in payload["ranked_candidates"] if r["strategy_mode"] == mode]
    before = _inventory(tmp_path)
    clock.now += timedelta(days=500)
    assert _verify(tmp_path) == report
    args = ["--runtime-root", str(tmp_path), "--run-id", "parity", "--account", "lx"]
    for _ in range(2):
        assert audit.main(args) == 0
        assert json.loads(capsys.readouterr().out) == report
    # Parent socket monkeypatch does not propagate: this is the real read-only
    # module CLI with explicit isolated config/runtime and no inherited OM secrets.
    cli_env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    cli_env.update(OM_ENV_FILE=str(tmp_path / "isolated.env"), OM_RUNTIME_ROOT=str(tmp_path),
                   OM_DATA_CONFIG=str(tmp_path / "portfolio.runtime.json"), OM_SECRET_BACKEND="env")
    cli = subprocess.run([sys.executable, "-B", "-m", "src.application.strategy_sample_verify", *args],
                         env=cli_env, capture_output=True, text=True, check=False, timeout=60)
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout) == report
    record_property("offline_cli_exit_code", cli.returncode)
    record_property("snapshot_sha256", report["snapshot_sha256"])
    record_property("manifest_sha256", report["manifest_sha256"])
    record_property("unchanged_input_inventory_sha256", canonical_sha256(before))
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("mode", ["put", "call"])
def test_multisymbol_full_tail_drift_from_pipeline(tmp_path, monkeypatch, mode):
    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch)
    baseline = _verify(tmp_path)
    assert baseline["status"] == "matched", baseline
    rows = [r for r in payload["ranked_candidates"] if r["strategy_mode"] == mode]
    assert len(rows) == 4
    representatives = [r["candidate_id"] for r in rows[:2]]
    rows[-1]["rank"], rows[-2]["rank"] = rows[-2]["rank"], rows[-1]["rank"]
    payload["ranked_candidates"] = sorted(payload["ranked_candidates"], key=lambda r: (r["strategy_mode"], r["rank"]))
    assert [r["candidate_id"] for r in payload["ranked_candidates"] if r["strategy_mode"] == mode][:2] == representatives
    _write_changed(tmp_path, payload)
    report = _verify(tmp_path)
    assert report["status"] == "mismatched", report
    order = next(c for c in report["checks"] if c["location"] == f"ranked_candidates/{mode}/order")
    assert order["status"] == "mismatched"
    assert order["recorded"][:2] == order["recomputed"][:2]
    assert order["recorded"][2:] == list(reversed(order["recomputed"][2:]))
    assert all(c["status"] == "matched" for c in report["checks"] if c["location"] != order["location"])


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("marker", [..., None, "ready", "unavailable_stale"])
def test_pipeline_fx_status_and_prepared_marker_presence(tmp_path, monkeypatch, stale, marker):
    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch, stale=stale, prepared_marker=marker)
    fx = payload["capacity_fx_evidence"]
    assert fx["observation_status"] == ("unavailable_stale" if stale else "ready")
    assert fx["marker_present"] is (stale or marker is not ...)
    if fx["marker_present"]:
        assert fx["marker_value"] == ("unavailable_stale" if stale else marker)
    else:
        assert "marker_value" not in fx
    assert (fx["usd_per_cny"], fx["cny_per_hkd"]) == (
        (None, None) if stale else (RATES.usd_per_cny, RATES.cny_per_hkd))
    contexts, errors = audit._contexts(tmp_path, payload)
    assert not errors
    assert ("_sell_put_fx_status" in contexts["portfolio"]) is (marker is not ...)
    if marker is not ...:
        assert contexts["portfolio"]["_sell_put_fx_status"] == marker
    report = _verify(tmp_path)
    assert not [c for c in report["checks"] if c["status"] == "mismatched"], report
    if stale:
        assert report["status"] == "insufficient_evidence", report
        # Native USD cash still supports numeric capacity; stale conversion cannot
        # invent CNY metrics or turn the missing scalar into a cash-block label.
        quantity = [c for c in report["checks"] if c["category"] == "quantity"]
        assert len(quantity) == 8 and all(c["status"] == "matched" and c["recomputed"] > 0 for c in quantity), report
        assert not any(c["category"] == "capacity_unavailable" for c in report["checks"])
        missing_cny = [c for c in report["checks"] if c["category"] == "metrics" and c["status"] == "insufficient_evidence"]
        assert len(missing_cny) == 8
        assert all(c["location"].endswith("/cny") and c["reason"] == "currency FX scalar missing" for c in missing_cny)
    else:
        assert report["status"] == "matched", report


@pytest.mark.parametrize("stale,present,value", [
    (False, True, None), (False, True, "unavailable_stale"),
    (True, False, None), (True, True, None), (True, True, "ready"),
])
def test_pipeline_sealed_marker_drift_cannot_become_capacity_input(tmp_path, monkeypatch, stale, present, value):
    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch, stale=stale)
    fx = payload["capacity_fx_evidence"]
    fx["marker_present"] = present
    if present:
        fx["marker_value"] = value
    else:
        fx.pop("marker_value", None)
    _write_changed(tmp_path, payload)
    report = _verify(tmp_path)
    assert report["status"] == "insufficient_evidence", report
    failures = [c for c in report["checks"] if c["category"] == "quantity" and c["status"] == "insufficient_evidence"]
    assert len(failures) == 4, report
    assert all("marker disagrees" in c["reason"] for c in failures)
    assert not any(c["category"] == "capacity_unavailable" for c in report["checks"])


def _write_invalid_evidence(base, payload):
    """Repair only envelope hashes so semantic corruption reaches the real reader."""
    payload["content_sha256"] = canonical_sha256({k: v for k, v in payload.items() if k != "content_sha256"})
    path = _snapshot_path(base, "parity", "lx")
    path.write_text(json.dumps(payload))
    manifest_path = path.parent / "candidate_snapshot_manifest.v4.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(e for e in manifest["owner_snapshots"] if e["candidate_owner"] == "opening")
    entry.update(content_sha256=payload["content_sha256"], sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    manifest["content_sha256"] = canonical_sha256({k: v for k, v in manifest.items() if k != "content_sha256"})
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("defect,reason", [
    ("fx_null", "capacity FX evidence shape"),
    ("observation_hash", "observation hash mismatch"),
    ("source_hash", "run dependency mismatch"),
    ("missing_marker", "evidence shape"),
    ("bad_time", "timezone-aware"),
    ("bool_version", "evidence shape"),
    ("scalar", "scalar mismatch"),
    ("ranking_null", "ranking"),
    ("duplicate_id", "ranking"),
    ("missing_id", "ranking"),
    ("cross_mode", "ranking"),
    ("profile", "ranking"),
])
def test_pipeline_malformed_evidence_rejected_before_replay(tmp_path, monkeypatch, defect, reason):
    from src.application.opening_candidate_snapshot import OpeningCandidateSnapshotError, validate_opening_candidate_snapshot

    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch)
    fx = payload["capacity_fx_evidence"]
    if defect == "fx_null":
        payload["capacity_fx_evidence"] = None
    elif defect == "observation_hash":
        fx["observation_sha256"] = "e" * 64
    elif defect == "source_hash":
        fx["run_fx_snapshot_sha256"] = "e" * 64
    elif defect == "missing_marker":
        fx.pop("marker_present")
    elif defect == "bad_time":
        fx["evaluated_at_utc"] = "2026-08-06T09:30:00"
    elif defect == "bool_version":
        fx["schema_version"] = True
    elif defect == "scalar":
        fx["usd_per_cny"] = .99
    elif defect == "ranking_null":
        payload["ranking_inputs"] = None
    elif defect == "duplicate_id":
        payload["ranking_inputs"]["put"]["candidate_ids"].append(payload["ranking_inputs"]["put"]["candidate_ids"][0])
    elif defect == "missing_id":
        payload["ranking_inputs"]["put"]["candidate_ids"].pop()
    elif defect == "cross_mode":
        payload["ranking_inputs"]["put"]["candidate_ids"][0] = payload["ranking_inputs"]["call"]["candidate_ids"][0]
    else:
        payload["ranking_inputs"]["put"]["profile"] = "unknown"
    _write_invalid_evidence(tmp_path, payload)
    with pytest.raises(OpeningCandidateSnapshotError, match=reason):
        validate_opening_candidate_snapshot(payload, expected_run_id="parity", expected_account="lx", verify_dependency_root=tmp_path)
    before = _inventory(tmp_path)
    report = _verify(tmp_path)
    assert report["status"] == "insufficient_evidence", report
    assert len(report["checks"]) == 1
    assert report["checks"][0]["category"] == "integrity"
    assert report["checks"][0]["status"] == "insufficient_evidence"
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("defect", ["missing", "corrupt", "wrong_run", "different_source", "forged_observation"])
def test_pipeline_requires_original_run_fx_source(tmp_path, monkeypatch, defect):
    from src.infrastructure.exchange_rates import project_exchange_rate_snapshot

    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch)
    path = tmp_path / "output_runs/parity/state/current_exchange_rates.v1.json"
    source = json.loads(path.read_text())
    if defect == "missing":
        path.unlink()
    elif defect == "corrupt":
        path.write_text("{")
    elif defect == "wrong_run":
        source["run_id"] = "other"
        path.write_text(json.dumps(source))
    else:
        source["snapshot"]["pairs"]["USDCNY"]["rate"] = 8.0
        source["snapshot_sha256"] = canonical_sha256(source["snapshot"])
        if defect == "different_source":
            path.write_text(json.dumps(source))
        else:
            fx = payload["capacity_fx_evidence"]
            fx["observation"] = project_exchange_rate_snapshot(source["snapshot"], purpose="capacity", now=NOW)
            fx["observation_sha256"] = canonical_sha256(fx["observation"])
            fx["usd_per_cny"] = 1 / 8
            _write_invalid_evidence(tmp_path, payload)
    before = _inventory(tmp_path)
    report = _verify(tmp_path)
    assert report["status"] == "insufficient_evidence", report
    assert len(report["checks"]) == 1
    assert report["checks"][0]["category"] == "integrity"
    from src.application.opening_candidate_snapshot import OpeningCandidateSnapshotError, load_opening_candidate_snapshot

    with pytest.raises(OpeningCandidateSnapshotError, match="run source"):
        load_opening_candidate_snapshot(base=tmp_path, run_id="parity", account="lx")
    assert report["checks"][0]["reason"] == "candidate owner snapshot is invalid: opening"
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("kind", ["portfolio", "ledger"])
def test_pipeline_both_prepared_fx_generations_are_bound(tmp_path, monkeypatch, kind):
    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch)
    dependency = next(d for d in payload["dependencies"] if d["kind"] == kind)
    manifest_path = tmp_path / dependency["relpath"]
    manifest = json.loads(manifest_path.read_text())
    key = "portfolio_context_relpath" if kind == "portfolio" else "payload_relpath"
    context_path = manifest_path.parent / manifest[key]
    context = json.loads(context_path.read_text())
    if kind == "portfolio":
        context["fx_snapshot_sha256"] = "e" * 64
    else:
        context["prepared_authority"]["run_fx_snapshot_sha256"] = "e" * 64
        manifest["run_fx_snapshot_sha256"] = "e" * 64
    raw = json.dumps(context).encode()
    digest = hashlib.sha256(raw).hexdigest()
    if kind == "portfolio":
        context_path = manifest_path.parent / f"portfolio_context.{digest}.json"
        manifest[key] = context_path.name
    context_path.write_bytes(raw)
    manifest["payload_sha256"] = digest
    manifest_path.write_text(json.dumps(manifest))
    dependency["sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    _write_changed(tmp_path, payload)
    contexts, errors = audit._contexts(tmp_path, payload)
    assert not errors, errors
    if kind == "portfolio":
        assert contexts[kind]["fx_snapshot_sha256"] == "e" * 64
    else:
        assert contexts[kind]["prepared_authority"]["run_fx_snapshot_sha256"] == "e" * 64
    before = _inventory(tmp_path)
    report = _verify(tmp_path)
    assert report["status"] == "insufficient_evidence", report
    failures = [c for c in report["checks"] if c["category"] == "quantity" and c["status"] == "insufficient_evidence"]
    assert len(failures) == 4, report
    assert all("not bound" in c["reason"] for c in failures)
    assert _inventory(tmp_path) == before


def test_pipeline_legacy_absence_keeps_partial_comparisons(tmp_path, monkeypatch):
    payload, _, _ = _produce_pipeline_sample(tmp_path, monkeypatch)
    payload.pop("capacity_fx_evidence")
    payload.pop("ranking_inputs")
    _write_changed(tmp_path, payload)
    report = _verify(tmp_path)
    assert report["status"] == "insufficient_evidence", report
    assert all(c["status"] == "matched" for c in report["checks"] if c["category"] in {"metrics", "filter"})
    assert len([c for c in report["checks"] if c["category"] == "quantity" and c["status"] == "insufficient_evidence"]) == 4
    tails = [c for c in report["checks"] if c["location"].endswith("/tail")]
    assert len(tails) == 2
    assert all(c["status"] == "insufficient_evidence" and "input order not archived" in c["reason"] for c in tails)
