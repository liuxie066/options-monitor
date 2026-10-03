from functools import partial
import json

import pytest

from src.application.ledger.manual_trades import persist_manual_open_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.multiplier_cache import save_cache
from src.application.trades.intake import process_trade_payload
from src.application.trades.normalizer import normalize_trade_deal
from src.application.trades.resolver import resolve_trade_deal
from src.application.trades.state import (
    append_trade_intake_audit,
    load_trade_intake_state,
    upsert_deal_state,
    write_trade_intake_state,
)


@pytest.fixture(params=[False, True], ids=["legacy", "canonical"])
def close_intake(tmp_path, request):
    repo = SQLiteOptionPositionsRepository(tmp_path / "positions.sqlite3")
    persist_manual_open_event(
        repo, broker="富途", account="lx", symbol="TIGR", option_type="put",
        side="short", contracts=2, currency="USD", strike=6.0, multiplier=100,
        expiration_ymd="2026-05-22", premium_per_share=0.2,
        opened_at_ms=1779129617118,
    )
    payload = {
        "deal_id": "recover-close", "futu_account_id": "REAL_1",
        "code": "US.TIGR260522P6000", "trd_side": "BUY_BACK",
        "qty": 2, "price": 0.0, "trade_time_ms": 1779468493916,
    }
    if request.param:
        payload.update(
            broker_account_id="futu-real-1", environment="REAL",
            external_id_namespace="futu.deal",
        )
    state_path = tmp_path / "state.json"
    process = partial(
        process_trade_payload, repo=repo, state_path=state_path,
        audit_path=tmp_path / "audit.jsonl", account_mapping={"REAL_1": "lx"},
        apply_changes=True, load_trade_intake_state_fn=load_trade_intake_state,
        write_trade_intake_state_fn=write_trade_intake_state,
        upsert_deal_state_fn=upsert_deal_state,
        append_trade_intake_audit_fn=append_trade_intake_audit,
        enrich_trade_payload_fn=None,
        normalize_trade_deal_fn=partial(
            normalize_trade_deal, repo_base=tmp_path, allow_opend_refresh=False,
        ),
        resolve_trade_deal_fn=resolve_trade_deal,
    )
    cache_path = tmp_path / "output_shared" / "state" / "multiplier_cache.json"
    return repo, payload, state_path, process, cache_path


@pytest.mark.parametrize("price", [0.0, 0.1])
def test_close_retries_after_multiplier_source_recovery_once(close_intake, price):
    repo, payload, state_path, process, cache_path = close_intake
    payload["price"] = price
    first = process(payload)
    assert first["status"] == "unresolved", first
    assert first["reason"] == "missing_required_fields:multiplier"
    state = load_trade_intake_state(state_path)
    unresolved = next(iter(state["unresolved_deal_ids"].values()))
    assert unresolved["retryable"] is True
    assert len(repo.list_trade_events()) == 1
    assert repo.list_trade_lifecycle_evidence() == []
    assert repo.list_trade_lifecycle_notifications() == []

    save_cache(cache_path, {"TIGR": {"multiplier": 100, "source": "restored"}})
    second = process(payload)
    assert second["status"] == "applied", second
    state = load_trade_intake_state(state_path)
    assert state["unresolved_deal_ids"] == {}
    assert len(state["processed_deal_ids"]) == 1
    events = repo.list_trade_events()
    evidence = repo.list_trade_lifecycle_evidence()
    notifications = repo.list_trade_lifecycle_notifications()
    assert len([row for row in events if row["event_type"] != "open"]) == 1
    assert len(notifications) == 1
    assert repo.list_position_lots()[0]["fields"]["contracts_open"] == 0

    third = process(payload)
    assert third["status"] == "skipped", third
    assert repo.list_trade_events() == events
    assert repo.list_trade_lifecycle_evidence() == evidence
    assert repo.list_trade_lifecycle_notifications() == notifications


@pytest.mark.parametrize("price", [0.0, 0.1])
@pytest.mark.parametrize("change", [{"qty": 1}, {"price": 0.3}])
def test_multiplier_recovery_preserves_other_economic_facts(close_intake, price, change):
    repo, payload, _state_path, process, cache_path = close_intake
    payload["price"] = price
    assert process(payload)["reason"] == "missing_required_fields:multiplier"
    save_cache(cache_path, {"TIGR": {"multiplier": 100, "source": "restored"}})
    result = process({**payload, **change})
    assert result["status"] == "failed", result
    assert result["reason"] == "broker_deal_economic_conflict"
    retried = process({**payload, **change}, retry_failed_deal=True)
    assert retried["reason"] == "broker_deal_economic_conflict"
    assert len(repo.list_trade_events()) == 1
    assert repo.list_trade_lifecycle_evidence() == []
    assert repo.list_trade_lifecycle_notifications() == []

    recovered = process(payload, retry_failed_deal=True)
    assert recovered["status"] == "applied", recovered
    assert len([row for row in repo.list_trade_events() if row["event_type"] != "open"]) == 1
    assert len(repo.list_trade_lifecycle_notifications()) == 1


@pytest.mark.parametrize("price", [0.0, 0.1])
@pytest.mark.parametrize("multiplier", [None, "", True, "100.00000000000000001"])
def test_explicit_invalid_multiplier_remains_non_retryable(close_intake, price, multiplier):
    repo, payload, state_path, process, cache_path = close_intake
    save_cache(cache_path, {"TIGR": {"multiplier": 100, "source": "restored"}})
    result = process({**payload, "price": price, "multiplier": multiplier})
    assert result["status"] == "unresolved", result
    assert result["reason"] == "execution_admission_failed"
    unresolved = next(iter(load_trade_intake_state(state_path)["unresolved_deal_ids"].values()))
    assert unresolved["retryable"] is False
    assert len(repo.list_trade_events()) == 1
    assert repo.list_trade_lifecycle_evidence() == []
    assert repo.list_trade_lifecycle_notifications() == []


@pytest.mark.parametrize("price", [0.0, 0.1])
def test_confirmed_multiplier_cannot_change_on_replay(close_intake, price):
    repo, payload, state_path, process, cache_path = close_intake
    payload["price"] = price
    save_cache(cache_path, {"TIGR": {"multiplier": 100, "source": "confirmed"}})
    assert process(payload)["status"] == "applied"
    events = repo.list_trade_events()
    evidence = repo.list_trade_lifecycle_evidence()
    notifications = repo.list_trade_lifecycle_notifications()
    confirmed_state = load_trade_intake_state(state_path)
    save_cache(cache_path, {"TIGR": {"multiplier": 500, "source": "changed"}})
    result = process(payload)
    assert result["status"] == "unresolved", result
    assert "unsupported:ledger_contract_multiplier" in result["diagnostics"]["errors"]
    assert load_trade_intake_state(state_path) == confirmed_state
    assert repo.list_trade_events() == events
    assert repo.list_trade_lifecycle_evidence() == evidence
    assert repo.list_trade_lifecycle_notifications() == notifications


@pytest.mark.parametrize("price", [0.0, 0.1])
@pytest.mark.parametrize("invalid", [
    {"qty": 0}, {"price": -1}, {"price": "NaN"},
    {"multiplier": None}, {"multiplier": "100.5"},
])
def test_invalid_replays_preserve_multiplier_recovery_state(close_intake, price, invalid):
    repo, payload, state_path, process, cache_path = close_intake
    payload["price"] = price
    assert process(payload)["reason"] == "missing_required_fields:multiplier"
    original_state = load_trade_intake_state(state_path)
    save_cache(cache_path, {"TIGR": {"multiplier": 100, "source": "restored"}})
    for _ in range(2):
        rejected = process({**payload, **invalid}, retry_failed_deal=True)
        assert rejected["status"] == "unresolved", rejected
        assert rejected["reason"] == "execution_admission_failed"
        assert load_trade_intake_state(state_path) == original_state
        assert len(repo.list_trade_events()) == 1
        assert repo.list_trade_lifecycle_evidence() == []
        assert repo.list_trade_lifecycle_notifications() == []
    assert "execution_admission_failed" in state_path.with_name("audit.jsonl").read_text()
    recovered = process(payload, retry_failed_deal=True)
    assert recovered["status"] == "applied", recovered
    assert load_trade_intake_state(state_path)["unresolved_deal_ids"] == {}
    assert len([row for row in repo.list_trade_events() if row["event_type"] != "open"]) == 1
    assert len(repo.list_trade_lifecycle_notifications()) == 1
    assert process(payload)["status"] == "skipped"
    assert len([row for row in repo.list_trade_events() if row["event_type"] != "open"]) == 1
    assert len(repo.list_trade_lifecycle_notifications()) == 1


@pytest.mark.parametrize("invalid", [{"qty": 0}, {"price": -1}, {"price": "NaN"}])
def test_initial_invalid_close_still_records_unresolved_state(close_intake, invalid):
    repo, payload, state_path, process, cache_path = close_intake
    save_cache(cache_path, {"TIGR": {"multiplier": 100, "source": "restored"}})
    rejected = process({**payload, **invalid})
    assert rejected["reason"] == "execution_admission_failed"
    unresolved = next(iter(load_trade_intake_state(state_path)["unresolved_deal_ids"].values()))
    assert unresolved["retryable"] is False
    assert unresolved["diagnostics"]["errors"] == rejected["diagnostics"]["errors"]
    assert len(repo.list_trade_events()) == 1


@pytest.mark.parametrize("price", [0.0, 0.1])
@pytest.mark.parametrize("change", [{"price": 0.3}, {"qty": 1}, {"qty": 0}, {"multiplier": None}])
def test_terminal_close_state_survives_conflicting_replays(close_intake, price, change):
    repo, payload, state_path, process, cache_path = close_intake
    payload["price"] = price
    save_cache(cache_path, {"TIGR": {"multiplier": 100, "source": "confirmed"}})
    assert process(payload)["status"] == "applied"
    confirmed = load_trade_intake_state(state_path)
    events = repo.list_trade_events()
    evidence = repo.list_trade_lifecycle_evidence()
    notifications = repo.list_trade_lifecycle_notifications()
    wrong_payload = {**payload, **change}
    for _ in range(2):
        rejected = process(wrong_payload, retry_failed_deal=True)
        assert rejected["status"] in {"failed", "unresolved"}, rejected
        assert load_trade_intake_state(state_path) == confirmed
        assert repo.list_trade_events() == events
        assert repo.list_trade_lifecycle_evidence() == evidence
        assert repo.list_trade_lifecycle_notifications() == notifications
    audit = [json.loads(line) for line in state_path.with_name("audit.jsonl").read_text().splitlines()]
    assert sum(item.get("payload") == wrong_payload for item in audit if item["phase"] == "received") == 2
    assert sum(item.get("reason") == rejected["reason"] for item in audit if item["phase"] in {"failed", "resolved"}) == 2
    replay = process(payload)
    assert replay["status"] == "skipped", replay
    assert len(load_trade_intake_state(state_path)["processed_deal_ids"]) == 1
    assert load_trade_intake_state(state_path)["unresolved_deal_ids"] == {}
    assert repo.list_trade_events() == events
    assert repo.list_trade_lifecycle_evidence() == evidence
    assert repo.list_trade_lifecycle_notifications() == notifications


def test_terminal_close_state_survives_resolver_read_failure(close_intake, monkeypatch):
    repo, payload, state_path, process, cache_path = close_intake
    save_cache(cache_path, {"TIGR": {"multiplier": 100, "source": "confirmed"}})
    assert process(payload)["status"] == "applied"
    confirmed = load_trade_intake_state(state_path)
    with monkeypatch.context() as patch:
        def unavailable():
            raise PermissionError("isolated read failure")
        patch.setattr(repo, "list_trade_events", unavailable)
        rejected = process(payload, retry_failed_deal=True)
    assert rejected["status"] == "failed"
    assert rejected["reason"] == "exception:PermissionError"
    assert load_trade_intake_state(state_path) == confirmed
    assert process(payload)["status"] == "skipped"


def test_valid_unresolved_close_can_advance_after_multiplier_recovery(close_intake):
    _repo, payload, state_path, process, cache_path = close_intake
    payload.update(price=0.1, qty=3)
    assert process(payload)["reason"] == "missing_required_fields:multiplier"
    prior = next(iter(load_trade_intake_state(state_path)["unresolved_deal_ids"].values()))
    save_cache(cache_path, {"TIGR": {"multiplier": 100, "source": "restored"}})
    result = process(payload)
    assert result["status"] == "unresolved"
    assert result["reason"].startswith("close_match_insufficient_contracts")
    current = next(iter(load_trade_intake_state(state_path)["unresolved_deal_ids"].values()))
    assert current["reason"] == result["reason"]
    assert current["attempt_count"] == prior["attempt_count"] + 1
    assert current["economic_payload_hash"] != prior["economic_payload_hash"]


@pytest.mark.parametrize("close_intake", [True], indirect=True, ids=["canonical"])
@pytest.mark.parametrize("price", [0.0, 0.1])
@pytest.mark.parametrize("change", [{"price": 0.3}, {"qty": 1}])
def test_conflict_preserves_source_baseline_after_ledger_commit_before_state(close_intake, price, change):
    repo, payload, state_path, process, cache_path = close_intake
    payload["price"] = price
    assert process(payload)["reason"] == "missing_required_fields:multiplier"
    pending = load_trade_intake_state(state_path)
    save_cache(cache_path, {"TIGR": {"multiplier": 100, "source": "restored"}})
    deal = normalize_trade_deal(
        payload, futu_account_mapping={"REAL_1": "lx"},
        repo_base=state_path.parent, allow_opend_refresh=False,
    )
    # Real ledger write with intake-state persistence interrupted afterward.
    assert resolve_trade_deal(deal, repo=repo, state=pending, apply_changes=True).status == "applied"
    assert load_trade_intake_state(state_path) == pending
    events = repo.list_trade_events()
    evidence = repo.list_trade_lifecycle_evidence()
    notifications = repo.list_trade_lifecycle_notifications()
    for _ in range(2):
        rejected = process({**payload, **change}, retry_failed_deal=True)
        assert rejected["reason"] == "trade_execution_economic_conflict", rejected
        assert load_trade_intake_state(state_path) == pending
        assert repo.list_trade_events() == events
        assert repo.list_trade_lifecycle_evidence() == evidence
        assert repo.list_trade_lifecycle_notifications() == notifications
    assert process(payload)["reason"] == "ledger_recorded"
    assert load_trade_intake_state(state_path)["unresolved_deal_ids"] == {}
    assert len(load_trade_intake_state(state_path)["processed_deal_ids"]) == 1
    assert repo.list_trade_events() == events
    assert repo.list_trade_lifecycle_notifications() == notifications


@pytest.mark.parametrize("with_receipt", [False, True])
def test_terminal_close_state_survives_normalization_failure(close_intake, with_receipt):
    repo, payload, state_path, process, cache_path = close_intake
    save_cache(cache_path, {"TIGR": {"multiplier": 100, "source": "confirmed"}})
    assert process(payload)["status"] == "applied"
    confirmed = load_trade_intake_state(state_path)
    events = repo.list_trade_events()

    def cannot_normalize(*_args, **_kwargs):
        raise ValueError("invalid replay input")

    receipt_calls = []

    def receipt(context):
        receipt_calls.append(context)
        return {"status": "sent", "delivery_confirmed": True}

    rejected = process(
        payload, normalize_trade_deal_fn=cannot_normalize,
        on_result_fn=receipt if with_receipt else None,
    )
    assert rejected["reason"] == "exception:ValueError"
    current = load_trade_intake_state(state_path)
    if with_receipt:
        assert len(receipt_calls) == 1
        assert current["failed_deal_ids"] == current["unresolved_deal_ids"] == {}
        key = next(iter(confirmed["processed_deal_ids"]))
        saved_receipt = current["processed_deal_ids"][key].pop("receipt")
        assert saved_receipt["delivery_confirmed"] is True
        assert saved_receipt["attempt_count"] == 1
    assert current == confirmed
    assert repo.list_trade_events() == events
    assert process(payload)["status"] == "skipped"
