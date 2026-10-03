from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from cash_evidence_helpers import cash_portfolio

from domain.domain.engine.candidate_engine import CandidateCalculationError, calculate_opening_candidate_metrics
from domain.domain.risk_capacity import (
    allocate_opening_share_capacity, allocate_portfolio_capacity_shadow,
    compute_sell_call_share_capacity, compute_short_call_locked_shares,
    compute_short_put_cash_secured,
)
from src.application.required_data_blobs import (
    RequiredDataBlobError, build_required_data_scan_blob_payload, canonical_scan_blob_bytes,
)

BAD = [True, "100.00000000000000001"]


def _row(multiplier=100):
    return {
        "symbol": "NVDA", "market": "US", "option_type": "put", "expiration": "2027-06-18",
        "dte": 43, "contract_symbol": "US.NVDA270618P00100000", "currency": "USD",
        "strike": 100., "spot": 110., "bid": 1., "ask": 1.01, "price_tick": .05,
        "implied_volatility": .3, "term_matched_rv": .2, "term_matched_rv_status": "ok",
        "underlier_observation_status": "ready", "option_standard_type": "STANDARD",
        "stock_owner": "US.NVDA", "stock_type": "DRVT", "chain_multiplier": multiplier,
        "snapshot_multiplier": multiplier, "multiplier": multiplier, "opening_contract_status": "ready",
        "snapshot_received_at_utc": datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.parametrize("raw", BAD)
@pytest.mark.parametrize("field", ["multiplier", "chain_multiplier", "snapshot_multiplier"])
def test_candidate_original_multiplier_rejected_before_fee_and_scanner_dto(raw, field):
    from src.application.scan_sell_put import compute_metrics

    row = {**_row(), field: raw}
    with pytest.raises(CandidateCalculationError):
        calculate_opening_candidate_metrics(row, mode="put")
    assert compute_metrics(pd.Series(row)) is None
    assert compute_metrics(pd.Series(_row(raw))) is None


@pytest.mark.parametrize("multiplier", [500, 1000])
def test_valid_actual_multiplier_survives_scanner_dto_and_fee_calculation(multiplier):
    from src.application.scan_sell_put import compute_metrics

    metrics = compute_metrics(pd.Series(_row(str(multiplier))))
    assert metrics["assignment_notional"] == 100 * multiplier
    assert metrics["gross_premium"] == 1.05 * multiplier
    assert metrics["net_income"] == round(metrics["gross_premium"] - metrics["estimated_full_sell_fees"], 6)


@pytest.mark.parametrize("raw", BAD + [500, 1000])
def test_risk_capacity_original_multiplier_at_all_quantity_boundaries(raw):
    valid = type(raw) is int
    capacity = compute_sell_call_share_capacity(shares_total=2000, shares_can_sell=2000, multiplier=raw)
    assert capacity.accepted is valid
    assert capacity.covered_contracts_available == (2000 // raw if valid else 0)
    assert compute_short_call_locked_shares(contracts_open=2, multiplier=raw) == (2 * raw if valid else None)
    assert compute_short_put_cash_secured(contracts_open=2, strike=100, multiplier=raw) == (200 * raw if valid else None)
    claim = {"account": "lx", "symbol": "NVDA", "claim_id": "cc", "strategy_family": "covered_call", "requested_contracts": 1, "multiplier": raw}
    allocations = allocate_opening_share_capacity(
        [{"account": "lx", "symbol": "NVDA", "status": "available", "shares_eligible": 2000, "shares_locked": 0, "shares_reserved": 0}],
        [claim],
    )
    assert allocations[0]["granted_shares"] == (raw if valid else 0)
    shadow = allocate_portfolio_capacity_shadow([{**claim, "shares_available_for_cover": 2000}])[0]
    assert shadow["allocated_contracts"] == int(valid)


def _blob(multiplier, *, raw_csv=False):
    provider = {k: _row()[k] for k in ("symbol", "option_type", "expiration", "contract_symbol", "strike")}
    provider["multiplier"] = None
    raw = (json.dumps({"symbol": "NVDA", "rows": [provider]}, ensure_ascii=False, indent=2) + "\n").encode()
    frame = pd.DataFrame([{**provider, "multiplier": multiplier if raw_csv else float(multiplier)}])
    output = io.StringIO()
    frame.to_csv(output, index=False)
    return build_required_data_scan_blob_payload(
        symbol="NVDA", market="US", raw_json_bytes=raw,
        required_data_csv_bytes=output.getvalue().encode(), columns=list(provider),
    )


@pytest.mark.parametrize("raw", BAD)
def test_blob_canonical_boundary_cannot_launder_raw_override(raw):
    payload = _blob(1 if raw is True else 100)
    payload["projection"]["multiplier_overrides"][0]["multiplier"] = raw
    with pytest.raises(RequiredDataBlobError, match="multiplier is invalid"):
        canonical_scan_blob_bytes(payload)


@pytest.mark.parametrize("raw", BAD)
def test_blob_builder_rejects_invalid_csv_multiplier_enrichment(raw):
    with pytest.raises(RequiredDataBlobError, match="multiplier enrichment is invalid"):
        _blob(raw, raw_csv=True)


@pytest.mark.parametrize("multiplier", [500, 1000])
def test_blob_preserves_actual_multiplier_and_existing_csv_contract(multiplier):
    payload = _blob(multiplier)
    encoded = canonical_scan_blob_bytes(payload)
    assert json.loads(encoded)["projection"]["multiplier_overrides"][0]["multiplier"] == multiplier


@pytest.mark.parametrize("raw", BAD + [pd.NA, 500, 1000])
@pytest.mark.parametrize("source", ["chain", "snapshot", "snapshot_alias"])
def test_opend_normalization_does_not_launder_original_multiplier(raw, source):
    from src.application.opening_quote_evidence import normalize_option_observation

    valid = type(raw) is int
    expected = raw if valid or raw is True else 100
    now = datetime(2026, 8, 6, 15, tzinfo=timezone.utc)
    code = "US.NVDA260821P00170000"
    chain = {"code": code, "lot_size": expected, "stock_type": "DRVT", "stock_owner": "US.NVDA", "option_standard_type": "STANDARD", "suspension": False}
    snapshot = {"code": code, "bid_price": 1., "ask_price": 1.2, "bid_vol": 4, "ask_vol": 6, "price_spread": .01, "option_contract_size": expected, "sec_status": "NORMAL", "suspension": False, "option_implied_volatility": 25., "option_delta": -.2}
    if source == "chain":
        chain["lot_size"] = raw
    elif source == "snapshot":
        snapshot["option_contract_size"] = raw
    else:
        snapshot["option_contract_multiplier"] = raw
    result = normalize_option_observation(
        expected_owner="US.NVDA", market="US", currency="USD", chain_row=chain,
        snapshot_row=snapshot, underlier_observation=SimpleNamespace(status="ready"),
        now_utc=now, snapshot_requested_at_utc=now.isoformat(), snapshot_received_at_utc=now.isoformat(),
    )
    assert (result.status == "ready") is valid
    assert result.multiplier == (raw if valid else None)


@pytest.mark.parametrize("raw", BAD + [500, 1000])
def test_sell_put_cash_facade_and_dataframe_keep_invalid_requirements_unavailable(raw):
    from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash, sell_put_opening_capacity_inputs
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    valid = type(raw) is int
    converter = CurrencyConverter(ExchangeRates())
    portfolio = cash_portfolio({"cash_by_currency": {"USD": 1_000_000}, "option_ctx": {"decision_snapshot_status": "trusted", "cash_secured_by_symbol_by_ccy": {}, "cash_secured_total_by_ccy": {}, "cash_secured_total_cny": 0}})
    capacity = sell_put_opening_capacity_inputs(symbol="NVDA", strike=100, multiplier=raw, currency="USD", portfolio_ctx=portfolio, exchange_rate_converter=converter)
    assert capacity["put_cash_capacity_available"] is valid
    assert capacity.get("put_cash_required") == (100 * raw if valid else None)
    for demo in (False, True):
        enriched = enrich_sell_put_candidates_with_cash(df_labeled=pd.DataFrame([_row(raw)]), symbol="NVDA", portfolio_ctx=portfolio, exchange_rate_converter=converter, demo_capacity=demo)
        value = enriched.iloc[0]["cash_required_native"]
        assert (value == 100 * raw) if valid else pd.isna(value)


@pytest.mark.parametrize("raw", ["100.00000000000000001", "True", "500", "1000"])
@pytest.mark.parametrize("source", ["csv", "frozen_wheel", "cc_lp", "combo", "coverage"])
def test_public_scan_validates_original_csv_multiplier(tmp_path, raw, source):
    from src.application.scan_sell_put import run_sell_put_scan
    from src.application.required_data_snapshot import FrozenRequiredDataBatch
    from src.application.wheel.scanning import _frames_from_snapshot

    row = _row(raw)
    csv_bytes = pd.DataFrame([row]).to_csv(index=False).encode()
    (tmp_path / "parsed").mkdir()
    (tmp_path / "parsed" / "NVDA_required_data.csv").write_bytes(csv_bytes)
    frames = None
    if source == "frozen_wheel":
        batch = FrozenRequiredDataBatch(
            manifest={}, manifest_bytes=b"{}", entries={"NVDA": ({}, csv_bytes)},
            unavailable={}, require_fresh=False,
        )
        frames, unavailable = _frames_from_snapshot(batch, ["NVDA"])
        assert not unavailable
        for field in ("multiplier", "chain_multiplier", "snapshot_multiplier"):
            assert frames["NVDA"].iloc[0][field] == raw
    if source in {"cc_lp", "combo", "coverage"}:
        from src.application.cc_lp_steps import _load_required_data_puts
        from src.application.sell_put_call_helper import _load_required_data
        from src.application.required_data_coverage import load_required_data_payload_from_csv

        if source == "coverage":
            payload = load_required_data_payload_from_csv(
                parsed=tmp_path / "parsed" / "NVDA_required_data.csv", symbol="NVDA",
            )
            frame = pd.DataFrame(payload["rows"])
        else:
            loader = _load_required_data_puts if source == "cc_lp" else _load_required_data
            frame = loader(input_root=tmp_path, symbol="NVDA")
        frames = {"NVDA": frame}
        for field in ("multiplier", "chain_multiplier", "snapshot_multiplier"):
            assert frame.iloc[0][field] == raw
    rejected = []
    result = run_sell_put_scan(
        symbols=["NVDA"], input_root=tmp_path, min_annualized_net_return=0.01,
        required_data_frames=frames, calculation_decision_sink_fn=rejected.extend,
    )
    if raw in {"500", "1000"}:
        assert len(result) == 1
        assert not rejected
        assert result.iloc[0]["assignment_notional"] == 100 * int(raw)
        assert result.iloc[0]["gross_premium"] == 1.05 * int(raw)
    else:
        assert result.empty
        assert len(rejected) == 1


@pytest.mark.parametrize("raw", ["100.00000000000000001", "True", "0", "-1", "NaN", "null", "bad", " "])
def test_multiplier_csv_enrichment_preserves_explicit_invalid_values(tmp_path, monkeypatch, raw):
    from src.application import multiplier_cache
    from src.application.multiplier_steps import apply_multiplier_cache_to_required_data_csv
    from src.application.required_data_coverage import load_required_data_payload_from_csv

    monkeypatch.setattr(multiplier_cache, "resolve_multiplier", lambda **_kwargs: 500)
    path = tmp_path / "parsed" / "NVDA_required_data.csv"
    path.parent.mkdir()
    rows = [_row(raw), {**_row("100.00000000000000001"), "multiplier": ""}]
    pd.DataFrame(rows).to_csv(path, index=False)
    apply_multiplier_cache_to_required_data_csv(base=tmp_path, required_data_dir=tmp_path, symbol="NVDA")
    payload = load_required_data_payload_from_csv(parsed=path, symbol="NVDA")
    assert payload["rows"][0]["multiplier"] == raw
    assert payload["rows"][1]["multiplier"] == "500.0"
    for index in range(2):
        assert payload["rows"][index]["chain_multiplier"] == rows[index]["chain_multiplier"]
        assert payload["rows"][index]["snapshot_multiplier"] == rows[index]["snapshot_multiplier"]


def test_blob_enrichment_preserves_other_multiplier_original_values():
    provider = _row("100.00000000000000001")
    provider["multiplier"] = None
    raw = (json.dumps({"symbol": "NVDA", "rows": [provider]}, ensure_ascii=False, indent=2) + "\n").encode()
    final_csv = pd.DataFrame([{**provider, "multiplier": 500.0}]).to_csv(index=False).encode()
    payload = build_required_data_scan_blob_payload(
        symbol="NVDA", market="US", raw_json_bytes=raw,
        required_data_csv_bytes=final_csv, columns=list(provider),
    )
    # Builder and canonical reader both materialize and check the exact CSV hash.
    assert canonical_scan_blob_bytes(payload)
    assert payload["provider_payload"]["rows"][0]["chain_multiplier"] == "100.00000000000000001"
