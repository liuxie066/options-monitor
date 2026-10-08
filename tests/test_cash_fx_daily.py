from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dataclasses import replace
from decimal import Decimal
import json
import sqlite3
from zoneinfo import ZoneInfo
import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.performance.cash_conversion import DAILY_CASH_FX_POLICY, DAILY_CASH_FX_METHOD, MARKET_CASH_FX_METHOD, fixed_cash_fx_fact, validate_observed_cash_conversion
from domain.domain.performance.models import EvidenceEnvelope, select_fx_rate
from src.application.cash_conversion import cash_fx_observation_facts, load_cash_fx_payload, build_cash_conversion
from src.application.ledger.api import backfill_cash_conversions, correct_superseded_cash_conversions
from src.application.ledger.order_fee_migration import enrich_order_fees
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.infrastructure import exchange_rates
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)


@pytest.fixture(autouse=True)
def _fixed_capture_clock(monkeypatch):
    monkeypatch.setattr("src.application.cash_conversion.utc_now_ms", lambda: ms("2026-09-10T12:00:00"))


def observation(rate: str = "7.2", *, quote: str = "2026-09-07T02:00:00+00:00", captured: str = "2026-09-07T02:00:01+00:00") -> dict:
    return {"source": "tencent_quote", "rates": {"USDCNY": rate, "HKDCNY": "0.92"}, "timestamp": quote,
            "quote_timestamps": {"USDCNY": quote, "HKDCNY": quote}, "observed_at": captured}


def test_cash_fx_fact_keeps_one_valid_pair_and_original_quote_time() -> None:
    payload = {
        "pairs": {
            "USDCNY": {
                "rate": 7.2,
                "source": "tencent_quote",
                "quote_at_utc": "2026-09-07T02:00:00+00:00",
                "observed_at_utc": "2026-09-07T02:00:01+00:00",
                "quality": "holiday_carried",
            },
            "HKDCNY": {"rate": 0.92, "source": "sina_quote"},
        },
        "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
    }
    facts = cash_fx_observation_facts(payload, observed_at_ms=ms("2026-09-10T12:00:00"))
    assert len(facts) == 1
    assert facts[0].base_currency == "USD"
    assert facts[0].effective_at_ms == int(datetime.fromisoformat("2026-09-07T02:00:00+00:00").timestamp() * 1000)
    assert facts[0].observed_at_ms == int(datetime.fromisoformat("2026-09-07T02:00:01+00:00").timestamp() * 1000)


def event(identity: str, at_ms: int) -> TradeEvent:
    return TradeEvent(
        event_id=identity, event_type="open", event_time_ms=at_ms,
        contract_key=ContractKey.from_values(broker="富途", account="lx", underlying_symbol="NVDA", option_type="put", strike=100, expiration_ymd="2026-09-18"),
        contracts=1, price=2, currency="USD", source="broker", multiplier=100, lot_id=f"lot-{identity}",
        raw_payload={
            # §9.2 step 3: the short put side travels as the trade side.
            "side": "sell",
            "futu_account_id": "123",
            "order_id": identity,
        },
    )


def test_real_cash_write_selects_event_time_quote_and_preserves_late_arrival(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    cache = tmp_path / "rate_cache.json"
    cache.write_text(json.dumps(observation()))
    persist_trade_event_object(repo, event("before-quote", ms("2026-09-07T08:00:00")))
    cache.write_text(json.dumps(observation("7.8", quote="2026-09-07T12:00:00+00:00", captured="2026-09-07T12:00:01+00:00")))
    persist_trade_event_object(repo, event("after-quote", ms("2026-09-07T23:00:00")))
    cache.write_text(json.dumps(observation("8", quote="2026-09-08T02:00:00+00:00", captured="2026-09-08T02:00:01+00:00")))
    # The cache can contain tomorrow's quote while a delayed trade has yesterday's economic date.
    persist_trade_event_object(repo, event("late", ms("2026-09-07T14:00:00")))
    conversions = {row["event_id"]: row["raw_payload"]["cash_conversions"]["option_trade_cash_gross"] for row in repo.list_trade_events()}
    assert conversions["before-quote"]["status"] == "pending"
    assert conversions["after-quote"]["fx_rate"] == "7.8"
    assert conversions["late"]["fx_rate"] == "7.2"
    for row in repo.list_trade_events():
        conversion = conversions[row["event_id"]]
        if conversion["status"] == "observed":
            assert conversion["method"] == MARKET_CASH_FX_METHOD
            assert validate_observed_cash_conversion(conversion, cash_fact_id=f"option_trade_cash_gross:{row['event_id']}", native_amount="200", native_currency="USD", effective_at_ms=row["event_time_ms"])[1] is None


def test_source_quote_date_cannot_be_refreshed_by_capture_and_shanghai_midnight_is_a_boundary(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    cache = tmp_path / "rate_cache.json"
    cache.write_text(json.dumps(observation(quote="2026-09-06T15:59:00+00:00", captured="2026-09-06T16:01:00+00:00")))
    persist_trade_event_object(repo, event("refetched-old", ms("2026-09-07T00:02:00")))
    row = repo.list_trade_events()[0]
    assert row["raw_payload"]["cash_conversions"]["option_trade_cash_gross"]["status"] == "pending"
    assert not [rate for rate in PerformanceEvidenceSQLiteRepository(repo.db_path).read_all().fx_rates if rate.quality.get("cash_fx_policy")]
    undated = observation()
    undated.pop("quote_timestamps")
    cache.write_text(json.dumps(undated))
    persist_trade_event_object(repo, event("undated", ms("2026-09-07T12:00:00")))
    assert repo.list_trade_events()[-1]["raw_payload"]["cash_conversions"]["option_trade_cash_gross"]["status"] == "pending"


def test_concurrent_quote_capture_is_idempotent(tmp_path) -> None:
    path = tmp_path / "evidence.sqlite3"
    now = ms("2026-09-07T23:00:00")
    def fix(rate: str):
        repository = PerformanceEvidenceSQLiteRepository(path)
        candidates = cash_fx_observation_facts(observation(rate), observed_at_ms=now)
        rates = repository.persist_cash_fx_observations(candidates, migrated_at_ms=now)
        return next(fact.rate for fact in rates if fact.base_currency == "USD")
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(fix, ["7.2"] * 4))
    assert len(set(results)) == 1
    assert len(PerformanceEvidenceSQLiteRepository(path).read_all().fx_rates) == 2


def test_fx_storage_readback_failure_rolls_back_trade_and_daily_rates(tmp_path, monkeypatch):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    (tmp_path / "rate_cache.json").write_text(json.dumps(observation()))
    original = PerformanceEvidenceSQLiteRepository._read_fx_rates_conn
    calls = []

    def fail_readback(self, conn):
        assert conn.in_transaction
        calls.append(conn)
        if len(calls) == 2:
            assert conn.execute("SELECT COUNT(*) FROM performance_fx_rate_facts").fetchone()[0] == 2
            raise sqlite3.OperationalError("injected FX readback failure")
        return original(self, conn)

    monkeypatch.setattr(PerformanceEvidenceSQLiteRepository, "_read_fx_rates_conn", fail_readback)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        persist_trade_event_object(repo, event("rollback", ms("2026-09-07T12:00:00")))
    assert len(calls) == 2 and calls[0] is calls[1]
    assert repo.list_trade_events() == []
    assert PerformanceEvidenceSQLiteRepository(repo.db_path).schema_state() == "not_initialized"


@pytest.mark.parametrize("damage", ["value", "column"])
def test_public_fx_value_failure_keeps_native_cash_but_storage_failure_aborts(tmp_path, damage):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    (tmp_path / "rate_cache.json").write_text(json.dumps(observation()))
    load_cash_fx_payload(repo)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE performance_fx_rate_facts SET rate_text='99'" if damage == "value" else
                     "ALTER TABLE performance_fx_rate_facts RENAME COLUMN raw_json TO missing_raw_json")
        before = tuple(conn.iterdump())
    assert load_cash_fx_payload(repo, persist=False) == {"fx_rate_facts": ()}
    incoming = event("fx-damaged", ms("2026-09-07T12:00:00"))
    if damage == "column":
        with pytest.raises(sqlite3.OperationalError, match="raw_json"):
            persist_trade_event_object(repo, incoming)
        with sqlite3.connect(repo.db_path) as conn:
            assert tuple(conn.iterdump()) == before
    else:
        persist_trade_event_object(repo, incoming)
        stored = repo.list_trade_events()[0]
        assert stored["price"] == "2"
        assert stored["raw_payload"]["cash_conversions"]["option_trade_cash_gross"]["status"] == "pending"


def test_backfill_rejects_future_quotes_without_writing_conversions(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    before_quote = event("backfill", ms("2026-09-07T08:00:00"))
    repo.upsert_trade_event(before_quote)
    rates = cash_fx_observation_facts(observation(), observed_at_ms=ms("2026-09-07T20:00:00"))
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    evidence.import_envelope(EvidenceEnvelope(fx_rates=rates), apply=True, migrated_at_ms=ms("2026-09-07T20:00:00"))
    before = repo.list_trade_events()
    assert select_fx_rate(rates, base_currency="USD", at_ms=before_quote.event_time_ms).fact is None
    for apply in (False, True):
        result = backfill_cash_conversions(repo, evidence, apply=apply, migrated_at_ms=ms("2026-09-08T12:00:00"))
        assert result.preview_conversion_count == result.migrated_conversion_count == 0
        assert result.unresolved
    assert repo.list_trade_events() == before
    assert len(evidence.read_all().fx_rates) == 2


@pytest.mark.parametrize("source,effective", [
    ("manual_correction", "2026-09-08T10:00:00"),
    ("realtime_snapshot", "2026-09-08T10:00:00"),
    ("realtime_snapshot", "2026-09-07T13:00:00"),
])
def test_future_correction_preserves_pending_cash_backfill(tmp_path, source, effective) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    row = event("before-correction", ms("2026-09-07T12:00:00"))
    repo.upsert_trade_event(row)
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    now = ms("2026-09-10T12:00:00")
    original = cash_fx_observation_facts(observation(), observed_at_ms=now)[0]
    evidence.import_envelope(EvidenceEnvelope(fx_rates=(original,)), apply=True, migrated_at_ms=now)
    before = repo.list_trade_events()
    preview = backfill_cash_conversions(repo, evidence, apply=False, migrated_at_ms=now)
    assert preview.preview_conversion_count == 1
    correction = replace(
        original, fact_id="future-correction", source_id="future-correction", source=source,
        rate=Decimal("7"), effective_at_ms=ms(effective), observed_at_ms=now,
        supersedes_fact_id=original.fact_id,
    )
    evidence.import_envelope(EvidenceEnvelope(fx_rates=(correction,)), apply=True, migrated_at_ms=now)
    preview = backfill_cash_conversions(repo, evidence, apply=False, migrated_at_ms=now)
    assert preview.preview_conversion_count == 1
    assert preview.changes[0].conversion["rate_evidence_fact_id"] == original.fact_id
    assert repo.list_trade_events() == before
    applied = backfill_cash_conversions(repo, evidence, apply=True, migrated_at_ms=now)
    assert applied.migrated_conversion_count == 1
    stored = repo.list_trade_events()
    conversion = stored[0]["raw_payload"]["cash_conversions"]["option_trade_cash_gross"]
    assert conversion["rate_evidence_fact_id"] == original.fact_id
    assert validate_observed_cash_conversion(
        conversion, cash_fact_id=f"option_trade_cash_gross:{row.event_id}",
        native_amount="200", native_currency="USD", effective_at_ms=row.event_time_ms,
    ) == (Decimal("1440"), None)
    assert backfill_cash_conversions(repo, evidence, apply=True, migrated_at_ms=now + 1).migrated_conversion_count == 0
    assert repo.list_trade_events() == stored


def test_market_correction_chain_applies_only_through_cash_event_time() -> None:
    now = ms("2026-09-10T12:00:00")
    original = cash_fx_observation_facts(observation(), observed_at_ms=now)[0]
    active = replace(
        original, fact_id="active-correction", source_id="active-correction", rate=Decimal("7"),
        effective_at_ms=ms("2026-09-07T12:00:00"), observed_at_ms=now,
        supersedes_fact_id=original.fact_id,
    )
    future = replace(
        active, fact_id="future-correction", source_id="future-correction", rate=Decimal("6"),
        effective_at_ms=ms("2026-09-07T13:00:00"), supersedes_fact_id=active.fact_id,
    )
    for instant, expected in (("2026-09-07T11:59:59", original), ("2026-09-07T12:00:00", active), ("2026-09-07T13:00:00", future)):
        conversion = build_cash_conversion(
            cash_fact_id="cash:chain", amount="200", currency="USD",
            fx_payload={"fx_rate_facts": (original, active, future)},
            effective_at_ms=ms(instant), observed_at_ms=now,
        )
        assert conversion["rate_evidence_fact_id"] == expected.fact_id
        assert validate_observed_cash_conversion(
            conversion, cash_fact_id="cash:chain", native_amount="200", native_currency="USD", effective_at_ms=ms(instant),
        ) == (Decimal(200) * expected.rate, None)
    # A corrected value for the same quote must still supersede the earlier capture.
    active = replace(active, effective_at_ms=original.effective_at_ms)
    conversion = build_cash_conversion(
        cash_fact_id="cash:same-quote", amount="200", currency="USD",
        fx_payload={"fx_rate_facts": (original, active, future)},
        effective_at_ms=original.effective_at_ms, observed_at_ms=now,
    )
    assert conversion["rate_evidence_fact_id"] == active.fact_id
    assert conversion["amount_cny"] == "1400"


def test_late_actual_fee_uses_original_day_and_missing_fx_does_not_block_fee(tmp_path) -> None:
    for available in (False, True):
        directory = tmp_path / str(available)
        repo = SQLiteOptionPositionsRepository(directory / "ledger.sqlite3")
        row = event("order-1", ms("2026-09-07T12:00:00"))
        repo.upsert_trade_event(row)
        if available:
            (directory / "rate_cache.json").write_text(json.dumps(observation()))
            load_cash_fx_payload(repo)
            (directory / "rate_cache.json").write_text(json.dumps(observation("8", quote="2026-09-08T02:00:00+00:00", captured="2026-09-08T02:00:01+00:00")))
        receipt = enrich_order_fees(repo, account="lx", target_identity=("富途", "lx", "123", "order-1"), actual_fees=({
            "broker": "富途", "account": "lx", "futu_account_id": "123", "order_id": "order-1", "fee_amount": "1.23", "currency": "USD", "event_kind": "option_trade", "dealt_quantity": "1", "observed_at_ms": ms("2026-09-08T12:00:00"),
        },), apply=True, applied_at_ms=ms("2026-09-08T12:00:01"))
        assert receipt["status_counts"] == {"committed": 1}
        stored = repo.list_trade_events()[0]
        assert Decimal(str(stored["fees"])) == Decimal("1.23")
        conversion = stored["raw_payload"]["cash_conversions"]["option_fee_cash"]
        assert conversion["status"] == ("observed" if available else "pending")
        if available:
            assert conversion["amount_cny"] == "-8.856" and conversion["method"] == MARKET_CASH_FX_METHOD


def test_fetch_preserves_quote_time_and_falls_back_when_tencent_date_is_old(monkeypatch) -> None:
    now = datetime.fromisoformat("2026-09-07T08:00:00+00:00")
    monkeypatch.setattr(exchange_rates, "_utc_now", lambda: now)
    text = 'v_whUSDCNY="310~美元~USDCNY~7.2~0~20260907150000~0";\nv_whHKDCNY="310~港币~HKDCNY~0.92~0~20260907150001~0";'
    monkeypatch.setattr(exchange_rates, "_http_get", lambda *_args, **_kwargs: text)
    payload = exchange_rates.fetch_market_exchange_rates()
    assert payload["timestamp"] == "2026-09-07T07:00:00+00:00"
    assert payload["observed_at"] == now.isoformat()
    assert payload["quote_timestamps"]["HKDCNY"] == "2026-09-07T07:00:01+00:00"
    usd_fields = ["15:01:00", "7.3", *(["0"] * 15), "2026-09-07"]
    hkd_fields = ["15:01:00", "0.93", *(["0"] * 15), "2026-09-07"]
    sina = (
        f'var hq_str_fx_susdcny="{",".join(usd_fields)}";\n'
        f'var hq_str_fx_shkdcny="{",".join(hkd_fields)}";'
    )
    monkeypatch.setattr(exchange_rates, "_http_get", lambda url, **_kwargs: text.replace("20260907", "20260906") if "gtimg" in url else sina)
    fallback = exchange_rates.fetch_market_exchange_rates()
    assert fallback["source"] == "sina_quote"
    assert fallback["timestamp"] == "2026-09-07T07:01:00+00:00"


def _daily_correction_case(tmp_path, original_event):
    """Freeze one day's rates, run the ordinary backfill, and expose what it produced."""
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    now = ms("2026-09-10T12:00:00")
    # A pre-upgrade daily snapshot may legitimately use a later quote that day.
    fixed = fixed_cash_fx_fact(cash_fx_observation_facts(observation(), observed_at_ms=now)[0])
    evidence.import_envelope(EvidenceEnvelope(fx_rates=(fixed,)), apply=True, migrated_at_ms=now)
    conversion = build_cash_conversion(
        cash_fact_id=f"option_trade_cash_gross:{original_event.event_id}", amount="200", currency="USD",
        fx_payload={"rates": {"USDCNY": str(fixed.rate)}, "timestamp": observation()["timestamp"]},
        effective_at_ms=original_event.event_time_ms, observed_at_ms=now,
        rate_source=fixed.source, rate_source_id=fixed.source_id, rate_evidence_fact_id=fixed.fact_id,
        method=DAILY_CASH_FX_METHOD,
    )
    repo.upsert_trade_event(replace(original_event, raw_payload={**original_event.raw_payload, "cash_conversions": {"option_trade_cash_gross": conversion}}))
    original = repo.list_trade_events()[0]
    before = original["raw_payload"]["cash_conversions"]["option_trade_cash_gross"]
    fixed = next(fact for fact in evidence.read_all().fx_rates if fact.fact_id == before["rate_evidence_fact_id"])
    return repo, evidence, now, original, before, fixed


@pytest.mark.parametrize("preserve_daily_quality", [False, True])
@pytest.mark.parametrize("correction_depth", [1, 2])
def test_public_daily_rate_correction_preview_apply_and_readback(tmp_path, preserve_daily_quality, correction_depth) -> None:
    original_event = event("correct-daily", ms("2026-09-07T08:00:00"))
    repo, evidence, now, original, before, fixed = _daily_correction_case(tmp_path, original_event)
    quality = dict(fixed.quality) if preserve_daily_quality else {"corrected": True}
    correction = replace(
        fixed, fact_id="daily-manual-correction", source_id="daily-manual-correction",
        source="manual_correction", rate=Decimal("7.0"), observed_at_ms=now,
        supersedes_fact_id=fixed.fact_id, quality=quality,
    )
    correction_facts = [fixed, correction]
    if correction_depth == 2:
        correction = replace(
            correction, fact_id="daily-second-correction", source_id="daily-second-correction",
            supersedes_fact_id=correction.fact_id, revision=2,
        )
        correction_facts.append(correction)
    evidence.import_envelope(EvidenceEnvelope(fx_rates=tuple(correction_facts)), apply=True, migrated_at_ms=now)

    ordinary_replay = backfill_cash_conversions(repo, evidence, apply=True, migrated_at_ms=now)
    assert ordinary_replay.migrated_conversion_count == 0
    preview = correct_superseded_cash_conversions(repo, evidence, apply=False, migrated_at_ms=now)
    assert preview.preview_conversion_count == 1
    assert repo.list_trade_events()[0] == original
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'cash_conversion_correction_audit'").fetchone() is None

    applied = correct_superseded_cash_conversions(repo, evidence, apply=True, migrated_at_ms=now)
    assert applied.migrated_conversion_count == 1
    reopened = SQLiteOptionPositionsRepository(repo.db_path)
    stored = reopened.list_trade_events()[0]
    after = stored["raw_payload"]["cash_conversions"]["option_trade_cash_gross"]
    assert after["fx_rate"] == "7"
    assert after["amount_cny"] == "1400"
    assert after["rate_source"] == "manual_correction"
    assert after["rate_evidence_fact_id"] == correction.fact_id
    assert validate_observed_cash_conversion(
        after, cash_fact_id="option_trade_cash_gross:correct-daily", native_amount="200",
        native_currency="USD", effective_at_ms=original_event.event_time_ms,
    )[1] is None
    assert {key: value for key, value in stored.items() if key != "raw_payload"} == {
        key: value for key, value in original.items() if key != "raw_payload"
    }
    assert next(fact for fact in evidence.read_all().fx_rates if fact.fact_id == fixed.fact_id) == fixed
    repeated = correct_superseded_cash_conversions(reopened, evidence, apply=True, migrated_at_ms=now + 1)
    assert repeated.migrated_conversion_count == 0
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cash_conversion_correction_audit").fetchone()[0] == 1


@pytest.mark.parametrize("cross_day", [False, True])
def test_public_daily_rate_correction_requires_same_day_supersedes_chain(tmp_path, cross_day) -> None:
    original_event = event("preserve-daily", ms("2026-09-07T08:00:00"))
    repo, evidence, now, original, before, fixed = _daily_correction_case(tmp_path, original_event)
    unrelated = replace(
        fixed, fact_id="unrelated-manual-rate", source_id="unrelated-manual-rate", source="manual_correction",
        rate=Decimal("7.0"), observed_at_ms=now, quality={"corrected": True},
        effective_at_ms=ms("2026-09-08T10:00:00") if cross_day else fixed.effective_at_ms,
        supersedes_fact_id=fixed.fact_id if cross_day else None,
    )
    evidence.import_envelope(EvidenceEnvelope(fx_rates=(fixed, unrelated)), apply=True, migrated_at_ms=now)
    preview = correct_superseded_cash_conversions(repo, evidence, apply=False, migrated_at_ms=now)
    assert preview.preview_conversion_count == 0
    applied = correct_superseded_cash_conversions(repo, evidence, apply=True, migrated_at_ms=now)
    assert applied.migrated_conversion_count == 0
    assert repo.list_trade_events()[0] == original
