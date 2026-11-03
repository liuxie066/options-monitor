"""Broker capacity through candidate rows, immutable seal and current readers."""
import pandas as pd
import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.risk_capacity import evaluate_cash_snapshot
from src.application.opening_candidate_snapshot import (
    OpeningCandidateSnapshotError, seal_opening_candidate_snapshot,
    validate_opening_candidate_snapshot, load_opening_candidate_snapshot,
)
from src.application.candidate_snapshot_manifest import load_candidate_snapshot_bundle
from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
from test_opening_candidate_snapshot import (
    NOW, _candidate, _dependencies, _scan_status, _physical_account, _publish_opening_manifest,
)


def _authority(**overrides):
    return {"schema_version": "physical_account_capacity_authority.v2", "status": "available",
            "logical_account": "lx", "broker_id": "longbridge", "broker_account_id": "LB-123",
            "trd_env": "REAL", "market": "us", "source": "longbridge",
            "credential_binding_sha256": "e" * 64,
            "binding_method": "user_confirmed_credentials_sha256", "broker_echoed": False, **overrides}


def _portfolio(**overrides):
    authority = _authority()
    context = {
        "portfolio_source_name": "longbridge", "capacity_authority": authority,
        "capacity_identity_hash": canonical_sha256(authority),
        "filters": {"account": "lx"}, "source_account_identifiers": ["LB-123"],
        "cash_source_observed_at": NOW.isoformat(), "cash_by_currency": {"USD": 29000},
        "cash_balance_reliable": True, "cash_balance_unavailable_by_row": {},
        "option_ctx": {"decision_snapshot_status": "trusted", "context_status": "available",
                       "cash_secured_total_by_ccy": {"USD": 2000},
                       "cash_secured_by_symbol_by_ccy": {"NVDA": {"USD": 2000}}},
        **overrides,
    }
    context["cash_snapshot"] = evaluate_cash_snapshot(
        context, expected_authority=authority, evaluated_at=NOW, max_age_sec=300,
    )
    return context


def _cash_row(context):
    return enrich_sell_put_candidates_with_cash(
        df_labeled=pd.DataFrame([_candidate(contract_symbol="NVDA260918P00090000", period_return=.04)]),
        symbol="NVDA", portfolio_ctx=context,
        exchange_rate_converter=CurrencyConverter(ExchangeRates()),
    ).iloc[0].to_dict()


def _seal(base, *, authority=None, row=None, run_id="broker", account="lx"):
    return seal_opening_candidate_snapshot(
        base=base, run_id=run_id, account=account, market="US",
        physical_account=authority if authority is not None else _authority(),
        account_config_sha256="a" * 64, strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(base, run_id, account),
        scan_statuses=[_scan_status("NVDA", "put", "completed")],
        final_candidates={"put": [row or _candidate(contract_symbol="NVDA260918P00090000", period_return=.04)]},
        run_mode={"scan_mode": "standard", "executable": True}, sealed_at=NOW,
    )


def test_longbridge_cash_capacity_rows_have_no_futu_identity():
    row = _cash_row(_portfolio())
    assert row["cash_free_effective_native"] == 27000
    assert row["max_new_contracts"] == 3
    assert row["broker_id"] == "longbridge"
    assert row["broker_account_id"] == "LB-123"
    assert "futu_account_id" not in row


@pytest.mark.parametrize("overrides", [
    {"source_account_identifiers": ["different"]},
    {"cash_source_observed_at": "2026-08-05T09:30:00+00:00"},
    {"capacity_authority": _authority(source="opend")},
])
def test_longbridge_cash_bad_evidence_still_fails_closed(overrides):
    row = _cash_row(_portfolio(**overrides))
    assert row["max_new_contracts"] == 0
    assert pd.isna(row["cash_free_effective_native"])


def test_longbridge_capacity_seals_and_manifest_loads(tmp_path):
    # Pandas missing values are presentation-only; the scanner normalizes them before capture.
    row = {key: value for key, value in _cash_row(_portfolio()).items() if value is not pd.NA}
    payload = _seal(tmp_path, row=row)
    assert payload["schema_version"] == "opening_candidate_snapshot.v4"
    assert payload["broker_id"] == "longbridge"
    assert payload["broker_account_id"] == "LB-123"
    assert "futu_account_id" not in payload
    assert payload["ranked_candidates"][0]["facts"]["max_new_contracts"] == 3
    manifest = _publish_opening_manifest(tmp_path, payload)
    assert load_opening_candidate_snapshot(base=tmp_path, run_id="broker", account="lx") == payload
    bundle = load_candidate_snapshot_bundle(base=tmp_path, run_id="broker", account="lx")
    assert bundle["owners"]["opening"] == payload
    from src.application.runtime_portfolio_snapshot import _validate_candidate_reference
    account_dir = tmp_path / "output_runs" / "broker" / "accounts" / "lx"
    references = [manifest["status_index"], *manifest["owner_snapshots"]]
    _validate_candidate_reference(
        manifest, binding={"schema_version": manifest["schema_version"], "content_sha256": manifest["content_sha256"]},
        chosen={key: manifest[key] for key in (
            "completion_reason", "expected_scopes", "expected_owners", "status_index", "owner_snapshots")},
        supplied={ref["relpath"]: (account_dir / ref["relpath"]).read_bytes() for ref in references},
        expected_run_id="broker", expected_account="lx", expected_account_config_sha256="a" * 64,
        expected_required_data_sha256=payload["required_data_manifest_sha256"],
    )


@pytest.mark.parametrize("mutate", [
    {"broker_account_id": "other"}, {"trade_env": "SIMULATE"},
    {"broker_id": "futu"}, {"futu_account_id": "LB-123"},
    {"credential_binding_sha256": "f" * 64}, {"binding_method": "broker_echo"}, {"broker_echoed": True},
    {"schema_version": "opening_candidate_snapshot.v3"},
])
def test_longbridge_seal_rejects_rebound_identity_even_with_recomputed_hash(tmp_path, mutate):
    payload = _seal(tmp_path)
    payload.update(mutate)
    payload["content_sha256"] = canonical_sha256({k: v for k, v in payload.items() if k != "content_sha256"})
    with pytest.raises(OpeningCandidateSnapshotError):
        validate_opening_candidate_snapshot(payload, expected_run_id="broker", expected_account="lx")


@pytest.mark.parametrize("overrides", [
    {"source": "opend"}, {"schema_version": "physical_account_capacity_authority.v1"},
    {"broker_account_id": ""}, {"logical_account": "sy"}, {"market": "hk"},
    {"futu_account_id": "LB-123"}, {"credential_binding_sha256": None},
    {"credential_binding_sha256": "E" * 64}, {"binding_method": "broker_echo"}, {"broker_echoed": True},
])
def test_longbridge_seal_rejects_invalid_authority(tmp_path, overrides):
    with pytest.raises(OpeningCandidateSnapshotError):
        _seal(tmp_path, authority=_authority(**overrides))


def test_futu_snapshot_shape_and_candidate_ids_stay_v3_and_v1(tmp_path):
    payload = _seal(tmp_path, authority=_physical_account("lx", "US"))
    assert payload["schema_version"] == "opening_candidate_snapshot.v3"
    assert payload["futu_account_id"] == "12345"
    assert "broker_id" not in payload and "broker_account_id" not in payload
    decision = payload["candidate_decisions"][0]
    normalized = decision["normalized_input"]
    assert decision["candidate_id"] == canonical_sha256({
        "schema": "opening_candidate_identity.v1", "account": "lx", "futu_account_id": "12345",
        "market": "US", "strategy_mode": "put", "symbol": normalized["symbol"],
        "contract_symbol": normalized["contract_symbol"], "expiration": normalized["expiration"],
        "strike": normalized["strike"],
    })


@pytest.mark.parametrize("available", [True, False])
def test_default_scan_capture_can_publish_longbridge_snapshot(tmp_path, monkeypatch, available):
    from test_pipeline_capture_status_routing import _run_default_capture, _symbol_config, RUN_ID
    context = _portfolio()
    if not available:
        context["capacity_authority"] = _authority(status="unavailable", broker_account_id=None)
    _run_default_capture(
        monkeypatch, tmp_path,
        symbols=[_symbol_config("NVDA", market="US", sell_put_enabled=True, sell_call_enabled=False, combo_enabled=False)],
        statuses=[{"symbol": "NVDA", "market": "US", "strategy_family": "sell_put",
                   "strategy_mode": "put", "status": "completed", "candidate_count": 0,
                   "reason": "no_candidate", "quote_snapshot_id": "c" * 64,
                   "quote_receipt_relpath": "quotes/NVDA/receipt.json"}],
        runtime_context=(context, {"exchange_rates": {}}),
    )
    payload = load_opening_candidate_snapshot(base=tmp_path, run_id=RUN_ID, account="lx")
    assert payload["broker_id"] == "longbridge"
    assert "futu_account_id" not in payload

    assert payload["capacity_authority_status"] == ("available" if available else "unavailable")
    if not available:
        assert payload["broker_account_id"] is None
        assert payload["opening_status"] == "data_unavailable"
        assert payload["ranked_candidates"] == []


def test_unavailable_longbridge_authority_cannot_seal_accepted_candidates(tmp_path):
    with pytest.raises(OpeningCandidateSnapshotError, match="cannot admit candidates"):
        _seal(tmp_path, authority=_authority(status="unavailable", broker_account_id=None))


def test_real_longbridge_adapter_without_binding_seals_unavailable(tmp_path):
    from test_longbridge_portfolio_context import Client, config
    from src.application.longbridge_portfolio_context import fetch_longbridge_portfolio_context
    from domain.domain.risk_capacity import broker_capacity_binding

    context = fetch_longbridge_portfolio_context(cfg=config(), account="demo", client=Client(), environ={})
    authority = context["capacity_authority"]
    assert authority["status"] == "unavailable"
    assert authority["credential_binding_sha256"] is None
    assert broker_capacity_binding(context) == {}
    payload = seal_opening_candidate_snapshot(
        base=tmp_path, run_id="unbound", account="demo", market="US", physical_account=authority,
        account_config_sha256="a" * 64, strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(tmp_path, "unbound", "demo"),
        scan_statuses=[_scan_status("NVDA", "put", "completed", reason="no_candidate")],
        final_candidates={"put": []}, run_mode={"scan_mode": "standard", "executable": True}, sealed_at=NOW,
    )
    assert payload["schema_version"] == "opening_candidate_snapshot.v4"
    assert payload["capacity_authority_status"] == "unavailable"
    assert payload["broker_account_id"] is None
    assert payload["credential_binding_sha256"] is None
    assert payload["opening_status"] == "data_unavailable"
    assert payload["ranked_candidates"] == []
    assert "futu_account_id" not in payload
    assert load_opening_candidate_snapshot(base=tmp_path, run_id="unbound", account="demo") == payload
    _publish_opening_manifest(tmp_path, payload)
    assert load_candidate_snapshot_bundle(base=tmp_path, run_id="unbound", account="demo")["owners"]["opening"] == payload
    with pytest.raises(OpeningCandidateSnapshotError, match="cannot admit candidates"):
        _seal(tmp_path, authority=authority, run_id="unbound-ranked", account="demo")
    with pytest.raises(OpeningCandidateSnapshotError, match="authority is unavailable"):
        _seal(tmp_path, authority={**authority, "status": "available"}, run_id="promoted", account="demo")


def test_longbridge_cc_prefilter_scanner_and_sealer(tmp_path, monkeypatch):
    from datetime import datetime
    from test_longbridge_portfolio_context import Client, stock, bound_config, binding_env
    from test_cc_spot_distance_contract import NOW as clock, _quote, _write_calendar, CONVERTER
    from src.application import longbridge_portfolio_context, scan_sell_call
    from src.application.prefilters import apply_prefilters
    from src.application.sell_call_steps import run_sell_call_scan_and_summarize

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock if tz is None else clock.astimezone(tz)

    monkeypatch.setattr(longbridge_portfolio_context, "datetime", FixedDatetime)
    monkeypatch.setattr(scan_sell_call, "datetime", FixedDatetime)
    _write_calendar(tmp_path)
    context = longbridge_portfolio_context.fetch_longbridge_portfolio_context(
        cfg=bound_config(), account="demo", environ=binding_env(),
        client=Client(rows=[stock("NVDA.US", quantity="300", available="250", cost="80")]),
    )
    context["cash_snapshot"] = {"evaluated_at": clock.isoformat()}
    cc = {"strategy": "insurance_underwriting", "enabled": True}
    filtered = apply_prefilters(symbol="NVDA", sp={}, cc=cc, want_put=False, want_call=True, portfolio_ctx=context)
    assert filtered.want_call, filtered.call_skip_reason
    finals, decisions = [], []
    summary = run_sell_call_scan_and_summarize(
        symbol="NVDA", symbol_cfg={}, cc=cc, stock=filtered.stock, portfolio_ctx=context,
        required_data_dir=tmp_path, required_data_frame=pd.DataFrame([_quote()]),
        locked_shares_status="available", locked_shares_by_symbol={"NVDA": 100},
        locked_shares_unavailable_by_symbol={}, exchange_rate_converter=CONVERTER,
        final_candidates_sink_fn=lambda mode, rows: finals.extend(rows),
        candidate_decisions_sink_fn=lambda mode, rows: decisions.extend(rows),
    )
    assert summary["candidate_count"] == 1
    assert finals[0]["broker_id"] == "longbridge"
    assert finals[0]["broker_account_id"] == "expected-123"
    assert finals[0]["covered_contracts_available"] == 1
    assert finals[0]["shares_available_for_cover"] == 150
    assert "futu_account_id" not in finals[0]
    payload = seal_opening_candidate_snapshot(
        base=tmp_path, run_id="cc", account="demo", market="US", physical_account=context["capacity_authority"],
        account_config_sha256="a" * 64, strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(tmp_path, "cc", "demo"),
        scan_statuses=[_scan_status("NVDA", "call", "completed")],
        final_candidates={"call": finals}, candidate_evaluations={"call": decisions},
        run_mode={"scan_mode": "standard", "executable": True}, sealed_at=clock,
    )
    assert payload["opening_status"] == "candidates_found"
    assert payload["ranked_candidates"][0]["facts"]["broker_id"] == "longbridge"
    assert "futu_account_id" not in payload
    _publish_opening_manifest(tmp_path, payload)
    assert load_candidate_snapshot_bundle(base=tmp_path, run_id="cc", account="demo")["owners"]["opening"] == payload


@pytest.mark.parametrize("field,value", [
    ("broker_id", "futu"), ("broker_account_id", "other"), ("account", "sy"),
    ("trd_env", "SIMULATE"), ("market", "hk"), ("capacity_identity_hash", "f" * 64),
    ("capacity_authority_status", "unavailable"),
])
def test_longbridge_cc_stock_identity_mismatch_never_reaches_scanner(tmp_path, monkeypatch, field, value):
    from src.application.sell_call_steps import run_sell_call_scan_and_summarize
    context = _portfolio()
    stock = {"symbol": "NVDA", "shares": 300, "can_sell_qty": 300, "avg_cost": 80,
             "broker_id": "longbridge", "broker_account_id": "LB-123", "account": "lx",
             "trd_env": "REAL", "market": "us", "capacity_identity_hash": context["capacity_identity_hash"],
             "capacity_authority_status": "available", field: value}
    monkeypatch.setattr("src.application.sell_call_steps.run_sell_call_scan",
                        lambda **kwargs: pytest.fail("mismatched stock reached scanner"))
    summary = run_sell_call_scan_and_summarize(
        symbol="NVDA", symbol_cfg={}, cc={}, stock=stock, portfolio_ctx=context,
        required_data_dir=tmp_path, exchange_rate_converter=CurrencyConverter(ExchangeRates()),
    )
    assert summary["_strategy_status"] == "unavailable"
    assert summary["_strategy_reason"] == "physical_account_capacity_stock_identity_mismatch"
