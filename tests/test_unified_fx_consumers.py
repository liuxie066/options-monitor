from datetime import datetime
from decimal import Decimal
import json

import pytest
from cash_evidence_helpers import cash_portfolio

from domain.domain.performance.cash_conversion import validate_observed_cash_conversion
from domain.domain.performance.models import EvidenceEnvelope
from src.application.cash_conversion import build_cash_conversion, cash_fx_observation_facts
from src.application.ledger.api import backfill_cash_conversions
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance.evidence_collection import collect_current_performance_evidence
from src.application.runtime_paths import runtime_root_scope
from src.application.wheel.capacity import revalidate_selected_wheel_put_candidate
from src.infrastructure import exchange_rates as fx
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository
from test_cash_fx_daily import event


def at(text):
    return datetime.fromisoformat(text + "+08:00")


def ms(text):
    return int(at(text).timestamp() * 1000)


def quote():
    return {"pairs": {pair: {
        "rate": rate, "source": "tencent_quote",
        "quote_at_utc": at("2026-09-30T14:00:00").isoformat(),
        "observed_at_utc": at("2026-09-30T14:00:01").isoformat(),
    } for pair, rate in (("USDCNY", 7.2), ("HKDCNY", 0.92))}}


@pytest.mark.parametrize("instant,quality", [
    ("2026-09-30T15:00:00", "fresh"),
    ("2026-10-07T20:00:00", "holiday_carried"),
    ("2026-10-08T09:29:00", "holiday_carried"),
    ("2026-10-08T09:30:00", None),
    ("2026-10-09T10:00:00", None),
    ("2027-01-01T10:00:00", None),
    ("2026-09-30T13:59:00", None),
])
def test_current_and_historical_consumers_share_quote_eligibility(instant, quality):
    facts = cash_fx_observation_facts(quote(), observed_at_ms=ms("2027-01-02T10:00:00"))
    current = fx.rates_for_purpose(quote(), purpose="capacity", now=at(instant))
    conversion = build_cash_conversion(
        cash_fact_id="gross:1", amount="100", currency="HKD",
        fx_payload={"fx_rate_facts": facts}, effective_at_ms=ms(instant),
        observed_at_ms=ms("2027-01-02T10:00:00"),
    )
    assert bool(current) == bool(quality)
    assert conversion["status"] == ("observed" if quality else "pending")
    if quality:
        assert conversion["rate_quote_quality"] == quality
        assert conversion["amount_cny"] == "92"
        assert conversion["rate_timestamp"] == "2026-09-30T06:00:00+00:00"
        assert validate_observed_cash_conversion(
            conversion, cash_fact_id="gross:1", native_amount="100", native_currency="HKD", effective_at_ms=ms(instant),
        ) == (Decimal("92"), None)
        tampered = {**conversion, "rate_observed_at_ms": ms("2027-01-03T10:00:00")}
        assert validate_observed_cash_conversion(
            tampered, cash_fact_id="gross:1", native_amount="100", native_currency="HKD", effective_at_ms=ms(instant),
        )[1] == "fx_provenance_invalid"


def test_read_only_compatibility_loader_uses_calendar_instead_of_age_cutoff(tmp_path, monkeypatch):
    cache = tmp_path / "rate_cache.json"
    cache.write_text(json.dumps(quote()))
    monkeypatch.setattr(fx, "_utc_now", lambda: at("2026-10-07T12:00:00"))
    assert fx.load_exchange_rate_info(cache_path=cache, max_age_hours=24)["rates"]["HKDCNY"] == 0.92
    monkeypatch.setattr(fx, "_utc_now", lambda: at("2026-10-08T09:30:00"))
    assert fx.load_exchange_rate_info(cache_path=cache, max_age_hours=24)["rates"] == {}


def test_holiday_backfill_preview_apply_replay_and_later_quote_preserve_snapshot(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(event("holiday", ms("2026-10-07T12:00:00")))
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    now = ms("2026-10-08T12:00:00")
    facts = cash_fx_observation_facts(quote(), observed_at_ms=now)
    evidence.import_envelope(EvidenceEnvelope(fx_rates=facts), apply=True, migrated_at_ms=now)
    before = repo.list_trade_events()
    preview = backfill_cash_conversions(repo, evidence, apply=False, migrated_at_ms=now)
    assert preview.preview_conversion_count == 1
    assert repo.list_trade_events() == before
    applied = backfill_cash_conversions(repo, evidence, apply=True, migrated_at_ms=now)
    assert applied.migrated_conversion_count == 1
    stored = repo.list_trade_events()
    assert stored[0]["raw_payload"]["cash_conversions"]["option_trade_cash_gross"]["rate_quote_quality"] == "holiday_carried"
    later = quote()
    for row in later["pairs"].values():
        row.update(rate=8, quote_at_utc=at("2026-10-08T10:00:00").isoformat(), observed_at_utc=at("2026-10-08T10:00:01").isoformat())
    evidence.import_envelope(EvidenceEnvelope(fx_rates=cash_fx_observation_facts(later, observed_at_ms=now)), apply=True, migrated_at_ms=now)
    assert backfill_cash_conversions(repo, evidence, apply=True, migrated_at_ms=now).migrated_conversion_count == 0
    assert repo.list_trade_events() == stored


def test_manual_capture_and_scheduled_capture_produce_identical_booking_evidence(tmp_path):
    from test_performance_evidence_collection import _position
    now = ms("2026-10-07T12:00:00")
    result = collect_current_performance_evidence(
        period_status="partial_current", refresh_quotes=True, option_positions=[_position()], now_ms=now,
        option_snapshot_rows_fetcher=lambda _: [], fx_payload_fetcher=quote,
    )
    scheduled = cash_fx_observation_facts(quote(), observed_at_ms=now)
    assert result.fx_rates == tuple(fact for fact in scheduled if fact.base_currency == "USD")
    evidence = PerformanceEvidenceSQLiteRepository(tmp_path / "evidence.sqlite3")
    evidence.import_envelope(result.envelope, apply=True, migrated_at_ms=now)
    evidence.persist_cash_fx_observations(scheduled, migrated_at_ms=now)
    assert len(evidence.read_all().fx_rates) == 2
    assert evidence.import_envelope(result.envelope, apply=True, migrated_at_ms=now).inserted_count == 0


def test_missing_source_observation_time_cannot_be_invented():
    payload = quote()
    for row in payload["pairs"].values():
        row.pop("observed_at_utc")
    with pytest.raises(ValueError, match="verified"):
        cash_fx_observation_facts(payload, observed_at_ms=ms("2026-10-07T12:00:00"))


def test_holiday_fee_revision_preserves_quote_evidence():
    from src.application.ledger.order_fee_migration import _conversion_for_amount
    instant = ms("2026-10-07T12:00:00")
    prior = build_cash_conversion(
        cash_fact_id="option_fee_cash:fee", amount="-1", currency="USD",
        fx_payload={"fx_rate_facts": cash_fx_observation_facts(quote(), observed_at_ms=instant)},
        effective_at_ms=instant, observed_at_ms=instant,
    )
    revised = _conversion_for_amount(
        fact_id="option_fee_cash:fee", amount=Decimal("-2"), currency="USD", effective_at_ms=instant,
        previous=prior, previous_amount=Decimal("-1"), applied_at_ms=ms("2026-10-09T12:00:00"),
    )
    assert validate_observed_cash_conversion(
        revised, cash_fact_id="option_fee_cash:fee", native_amount="-2", native_currency="USD", effective_at_ms=instant,
    ) == (Decimal("-14.4"), None)
    for field in ("rate_timestamp", "rate_source_id", "rate_evidence_fact_id", "fx_calendar", "rate_quote_quality", "rate_observed_at_ms"):
        assert revised[field] == prior[field]


def test_conflicting_quote_capture_rolls_back_without_replacing_existing_fact(tmp_path):
    from dataclasses import replace
    now = ms("2026-10-07T12:00:00")
    evidence = PerformanceEvidenceSQLiteRepository(tmp_path / "evidence.sqlite3")
    facts = cash_fx_observation_facts(quote(), observed_at_ms=now)
    evidence.persist_cash_fx_observations(facts, migrated_at_ms=now)
    before = evidence.read_all().fx_rates
    with pytest.raises(ValueError, match="conflict"):
        evidence.persist_cash_fx_observations((replace(facts[0], rate=Decimal("8")),), migrated_at_ms=now)
    assert evidence.read_all().fx_rates == before


def test_wheel_rechecks_sealed_fx_at_reopening_and_keeps_native_capacity(monkeypatch):
    clock = [at("2026-10-08T09:29:00")]
    monkeypatch.setattr(fx, "_utc_now", lambda: clock[0])
    fact = cash_portfolio({
        "account": "lx", "status": "available", "cash_by_currency": {"CNY": 100000},
        "cash_authority": {"status": "available", "logical_account": "lx"}, "cash_authority_hash": "authority",
        "cash_secured_by_currency": {}, "wheel_intent_reservations": [],
        "fx_snapshot": fx.project_exchange_rate_snapshot(quote(), purpose="capacity"),
    })
    candidate = {"claim_id": "wheel:put:branch", "wheel_branch_id": "branch", "symbol": "0700.HK",
                 "currency": "HKD", "strike": 420, "multiplier": 100, "granted_contracts": 1}
    assert revalidate_selected_wheel_put_candidate(cash_capacity_fact=fact, final_candidate=candidate)["granted_contracts"] == 1
    clock[0] = at("2026-10-08T09:30:00")
    with pytest.raises(ValueError, match="no longer has cash capacity"):
        revalidate_selected_wheel_put_candidate(cash_capacity_fact=fact, final_candidate=candidate, exchange_rate_converter=lambda *_: 100000)
    fact["cash_by_currency"] = {"HKD": 100000}
    assert revalidate_selected_wheel_put_candidate(cash_capacity_fact=fact, final_candidate=candidate)["granted_contracts"] == 1


def test_tool_cash_query_uses_runtime_shared_cache_with_custom_output(tmp_path, monkeypatch):
    from src.application import cash_headroom_query as query
    from src.application.agent_tools.materialization_impl import query_cash_headroom_tool
    runtime = tmp_path / "runtime"
    cache = fx.shared_exchange_rate_cache_path(runtime)
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps(quote()))
    before = cache.read_bytes()
    monkeypatch.setattr(fx, "_utc_now", lambda: at("2026-10-07T12:00:00"))
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda: None)
    captured = []
    def portfolio(**kwargs):
        captured.append(kwargs)
        return cash_portfolio({"cash_by_currency": {"HKD": 100}, "cash_balance_reliable": True})
    monkeypatch.setattr(query, "load_account_portfolio_context", portfolio)
    monkeypatch.setattr(query, "_load_option_position_records", lambda _: (object(), []))
    monkeypatch.setattr(query, "decision_state_snapshot", lambda *a, **kw: {})
    monkeypatch.setattr(query, "build_option_positions_context", lambda *a, **kw: {"decision_snapshot_status": "trusted", "cash_secured_total_by_ccy": {}, "cash_secured_total_cny": 0})
    monkeypatch.setattr(query, "resolve_data_config_path", lambda **kw: tmp_path / "data.json")
    with runtime_root_scope(runtime):
        result, _, _ = query_cash_headroom_tool(
            {"account": "lx", "output_dir": str(tmp_path / "custom")},
            load_runtime_config=lambda **kw: (runtime / "config.hk.json", {}),
            resolve_public_data_config_path=lambda *a: tmp_path / "data.json", normalize_broker=lambda _: "富途",
            resolve_output_root=lambda _: tmp_path / "custom", query_sell_put_cash=query.query_sell_put_cash,
            repo_base=lambda: tmp_path / "code", mask_path=str,
        )
    assert captured[0]["exchange_rate_cache_path"] == cache
    assert captured[0]["exchange_rate_observation"]["rates"]["HKDCNY"] == 0.92
    assert result["cash_available_total_cny"] == 92
    assert cache.read_bytes() == before
    assert not (tmp_path / "custom").exists()
