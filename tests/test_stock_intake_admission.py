from dataclasses import replace
from functools import partial

import pytest

from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.intake import process_trade_payload
from src.application.trades.normalizer import canonical_trade_execution_content, normalize_trade_deal
from src.application.trades.resolver import resolve_trade_deal
from src.application.trades.state import append_trade_intake_audit, load_trade_intake_state, upsert_deal_state, write_trade_intake_state


def _stock() -> dict:
    return {
        "broker_account_ref": {"broker_account_id": "futu:REAL:123", "broker_id": "futu", "external_account_id": "123", "environment": "REAL", "account_label": "lx"},
        "instrument_ref": {"asset_type": "stock", "symbol": "NVDA", "market": "US", "currency": "USD"},
        "external_id_namespace": "futu.deal", "external_execution_id": "stock-1",
        "side": "buy", "quantity": "0.5", "price": "100", "currency": "USD",
        "occurred_at_utc": "2026-09-07T02:30:00Z",
    }


def _process(tmp_path, payload, *, normalizer=normalize_trade_deal, source="push"):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    result = process_trade_payload(
        payload, repo=repo, state_path=tmp_path / "state.json", audit_path=tmp_path / "audit.jsonl",
        account_mapping={"123": "lx"}, apply_changes=True,
        load_trade_intake_state_fn=load_trade_intake_state, write_trade_intake_state_fn=write_trade_intake_state,
        upsert_deal_state_fn=upsert_deal_state, append_trade_intake_audit_fn=append_trade_intake_audit,
        enrich_trade_payload_fn=None, normalize_trade_deal_fn=partial(normalizer, allow_opend_refresh=False),
        resolve_trade_deal_fn=resolve_trade_deal, portfolio_management_enabled=True, source=source,
    )
    assert repo.list_trade_events() == []
    return result


@pytest.mark.parametrize("missing", ["quantity", "price", "occurred_at_utc", "currency", "side", "external_execution_id", "external_id_namespace"])
def test_missing_standard_stock_fact_is_reviewed_without_pm_refresh(tmp_path, missing):
    payload = _stock()
    payload.pop(missing)
    result = _process(tmp_path, payload)
    assert result["status"] == "unresolved"
    assert result["reason"] == "execution_admission_failed"
    assert "portfolio_refresh_intent" not in result


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN"])
def test_invalid_stock_quantity_never_refreshes_pm(tmp_path, quantity):
    result = _process(tmp_path, {**_stock(), "quantity": quantity})
    assert result["status"] == "unresolved"
    assert "portfolio_refresh_intent" not in result


def test_valid_fractional_stock_refresh_keeps_once_semantics(tmp_path):
    first = _process(tmp_path, _stock())
    replay = _process(tmp_path, _stock(), source="backfill")
    assert first["reason"] == "not_option_deal"
    assert first["portfolio_refresh_intent"]["account"] == "lx"
    assert "portfolio_refresh_intent" not in replay
    assert replay["reason"] == "duplicate_deal_id"


def test_empty_error_list_cannot_bypass_required_stock_facts(tmp_path):
    def incomplete(payload, **kwargs):
        deal = normalize_trade_deal(payload, **kwargs)
        execution = {**deal.execution_input, "quantity": None, "errors": []}
        return replace(deal, execution_input=execution)

    result = _process(tmp_path, _stock(), normalizer=incomplete)
    assert "portfolio_refresh_intent" not in result


@pytest.mark.parametrize("multiplier", [None, "0", "1.5"])
def test_standard_option_multiplier_never_reaches_writer_when_invalid(tmp_path, multiplier):
    payload = _stock()
    payload["instrument_ref"] = {**payload["instrument_ref"], "asset_type": "option", "option_type": "put", "strike": "100", "expiration_ymd": "2026-09-18", "multiplier": multiplier}
    payload.update(quantity="1", side="sell", position_effect="open")
    result = _process(tmp_path, payload)
    assert result["status"] == "unresolved"
    assert result["reason"] == "execution_admission_failed"
    assert "portfolio_refresh_intent" not in result


def _futu_option() -> dict:
    return {"acc_id": "123", "broker_account_id": "futu:REAL:123", "environment": "REAL", "internal_account": "lx", "external_id_namespace": "futu.deal", "deal_id": "option-1", "code": "US.NVDA260918P100000", "qty": "1", "price": "2", "trd_side": "SELL_SHORT", "create_time": "2026-09-07 10:30:00"}


@pytest.mark.parametrize("field,value", [("multiplier", "1.5"), ("contract_multiplier", "0"), ("lot_size", "bad"), ("multiplier", "NaN"), ("multiplier", False), ("multiplier", "-1")])
def test_invalid_futu_multiplier_stays_review_evidence_without_lookup(tmp_path, monkeypatch, field, value):
    def unexpected_lookup(**kwargs):
        pytest.fail("invalid source multiplier must not query provider or cache")

    monkeypatch.setattr("src.application.trades.normalizer.resolve_multiplier_with_source_and_diagnostics", unexpected_lookup)
    payload = {**_futu_option(), field: value}
    deal = normalize_trade_deal(payload, futu_account_mapping={"123": "lx"}, allow_opend_refresh=True)
    assert deal.raw_payload[field] == value
    assert deal.multiplier is None
    assert any(error.startswith("invalid:instrument_ref.multiplier:") for error in deal.execution_input["errors"])
    result = _process(tmp_path, payload)
    assert result["status"] == "unresolved"
    assert result["reason"] == "execution_admission_failed"


def test_valid_futu_multiplier_preserves_payload_authority_over_cache(monkeypatch):
    def unexpected_cache(*args, **kwargs):
        pytest.fail("valid explicit source multiplier must not be replaced by cache")

    monkeypatch.setattr("src.application.multiplier_cache.load_cache", unexpected_cache)
    deal = normalize_trade_deal({**_futu_option(), "multiplier": "10"}, futu_account_mapping={"123": "lx"}, allow_opend_refresh=True)
    assert deal.multiplier == 10
    assert deal.multiplier_source == "payload"
    assert deal.execution_input["instrument_ref"]["multiplier"] == "10"
    assert deal.execution_input["errors"] == []


@pytest.mark.parametrize("alias_value", ["1.5", "200"])
def test_conflicting_multiplier_alias_is_rejected_before_cache_and_comparison(tmp_path, monkeypatch, alias_value):
    def unexpected_lookup(**kwargs):
        pytest.fail("source multiplier alias conflict must not query provider or cache")

    monkeypatch.setattr("src.application.trades.normalizer.resolve_multiplier_with_source_and_diagnostics", unexpected_lookup)
    payload = {**_futu_option(), "multiplier": "100", "contract_multiplier": alias_value}
    assert canonical_trade_execution_content(payload)["errors"]
    result = _process(tmp_path, payload)
    assert result["status"] == "unresolved"
    assert result["reason"] == "execution_admission_failed"
