from __future__ import annotations

from types import SimpleNamespace
from dataclasses import replace

import pytest

from src.application.ledger.api import broker_execution_identity, broker_external_event_key
from src.application.trades.deal_identity import (
    completed_ledger_deal_keys,
    structured_deal_keys_from_assigned_stock_event,
    completed_ledger_execution_events,
)

from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    contract_key,
    contract_strike_key,
    normalize_contract_expiration,
    normalize_contract_option_type,
    normalize_position_effect,
    normalize_trade_side,
)


def test_trade_side_and_position_effect_aliases_are_centralized() -> None:
    assert normalize_trade_side("SELL_SHORT") == "sell"
    assert normalize_trade_side("sell short") == "sell"
    assert normalize_trade_side("short sell") == "sell"
    assert normalize_trade_side("buy to close") == "buy"
    assert normalize_trade_side("buy back") == "buy"
    assert normalize_trade_side("买平") == "buy"
    assert normalize_trade_side("买 平") == "buy"
    assert normalize_position_effect("SELL_SHORT") == "open"
    assert normalize_position_effect("sell short") == "open"
    assert normalize_position_effect("short sell") == "open"
    assert normalize_position_effect("buy to close") == "close"
    assert normalize_position_effect("buy back") == "close"
    assert normalize_position_effect("买 平") == "close"
    assert normalize_position_effect("voided") == "void"
    assert normalize_position_effect("adjustment") == "adjust"


def test_contract_expiration_accepts_common_option_date_shapes() -> None:
    assert normalize_contract_expiration("260618") == "2026-06-18"
    assert normalize_contract_expiration("20260618") == "2026-06-18"
    assert normalize_contract_expiration("2026-06-18T09:30:00") == "2026-06-18"
    assert normalize_contract_expiration("1781712000000") == "2026-06-18"
    assert normalize_contract_expiration("bad", fallback_raw=True) == "bad"
    assert normalize_contract_expiration("bad") is None


def test_contract_key_uses_canonical_symbol_option_type_expiration_and_strike() -> None:
    assert canonical_contract_symbol("HK.00700") == "0700.HK"
    assert normalize_contract_option_type("认沽") == "put"
    assert contract_strike_key("100") == "100.000000"
    assert contract_strike_key(float("nan")) == ""
    assert contract_key("HK.00700", "认沽", "260618", "100") == (
        "0700.HK",
        "put",
        "2026-06-18",
        "100.000000",
    )


def test_execution_identity_scopes_physical_account_environment_and_namespace() -> None:
    def deal(*, label="lx", physical="000123", environment="REAL", namespace="futu.deal"):
        return SimpleNamespace(
            internal_account=label, futu_account_id=physical, deal_id="42",
            execution_input={
                "broker_account_ref": {"broker_id": "futu", "external_account_id": physical, "environment": environment, "account_label": label},
                "external_id_namespace": namespace, "external_execution_id": "42",
            },
        )
    identity = broker_execution_identity(deal())
    assert identity == broker_execution_identity(deal(label="renamed"))
    assert identity != broker_execution_identity(deal(physical="123"))
    assert identity != broker_execution_identity(deal(environment="SIMULATE"))
    assert identity != broker_execution_identity(deal(namespace="futu.hk.deal"))
    assert broker_external_event_key(deal()) == "futu:lx:000123:42"


def test_ledger_completion_never_uses_an_unscoped_deal_id() -> None:
    unscoped = {"event_id": "old", "account": "lx", "raw_payload": {"source_deal_id": "42"}}
    assert completed_ledger_deal_keys([unscoped]) == set()
    rows = [
        {"event_id": f"split-{index}", "account": "lx", "contracts": 1, "raw_payload": {
            "source_deal_id": "42", "futu_account_id": "123",
            "broker_deal_completion": {"split_index": index, "split_count": 2, "allocated_contracts": 1, "expected_contracts": 2},
        }}
        for index in (1, 2)
    ]
    assert completed_ledger_deal_keys(rows[:1]) == set()
    assert completed_ledger_deal_keys(rows) == {"futu:lx:123:42"}
    assert structured_deal_keys_from_assigned_stock_event({"account": "sy", "futu_account_id": "999", "source_deal_id": "42"}) == {"futu:sy:999:42"}


def test_standard_execution_keeps_futu_event_lot_and_frozen_economics(tmp_path) -> None:
    from src.application.ledger.api import record_normalized_trade_event
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.normalizer import normalize_trade_deal

    payload = {
        "acc_id": "123", "broker_account_id": "futu:REAL:123", "environment": "REAL",
        "external_id_namespace": "futu.deal", "external_order_namespace": "futu.order",
        "deal_id": "42", "order_id": "order-42", "code": "US.NVDA260918P00100000",
        "qty": "1", "price": "2.50", "multiplier": "100", "trd_side": "SELL_SHORT",
        "create_time": "2026-09-07 10:30:00",
    }
    deal = normalize_trade_deal(payload, futu_account_mapping={"123": "lx"}, allow_opend_refresh=False)
    assert deal.execution_input["errors"] == []
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    first = record_normalized_trade_event(repo, deal)
    before_events, before_lots = repo.list_trade_events(), repo.list_position_lots()
    account = {**deal.execution_input["broker_account_ref"], "account_label": "renamed"}
    replay = replace(deal, internal_account="renamed", execution_input={**deal.execution_input, "broker_account_ref": account, "evidence_refs": ["history"]})
    second = record_normalized_trade_event(repo, replay)
    assert first.to_dict()["event_id"] == second.to_dict()["event_id"] == "futu:lx:123:42"
    assert second.to_dict()["created"] is False
    assert repo.list_trade_events() == before_events
    assert repo.list_position_lots() == before_lots
    assert completed_ledger_execution_events(before_events, replay) == before_events
    conflict = replace(deal, price=3.0, execution_input={**deal.execution_input, "price": "3"})
    with pytest.raises(ValueError, match="trade_execution_economic_conflict"):
        record_normalized_trade_event(repo, conflict)
    assert repo.list_trade_events() == before_events
    missing_identity = {**before_events[0], "event_id": "another-event", "raw_payload": {
        key: value for key, value in before_events[0]["raw_payload"].items() if key != "execution_id"
    }}
    with pytest.raises(ValueError, match="trade_execution_identity_metadata_mismatch"):
        repo.upsert_trade_event(missing_identity)
    assert repo.list_trade_events() == before_events


@pytest.mark.parametrize("change, error", [
    ({"acc_id": "456", "broker_account_id": "futu:REAL:456"}, "multiple_physical_accounts_in_projection"),
    ({"multiplier": "10"}, "ledger_contract_multiplier"),
])
def test_real_intake_admission_preserves_projection_scope(tmp_path, change, error):
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.auto_intake import _process_payload

    payload = {"acc_id": "123", "broker_account_id": "futu:REAL:123", "environment": "REAL",
               "external_id_namespace": "futu.deal", "external_order_namespace": "futu.order",
               "deal_id": "first", "order_id": "first-order", "code": "US.NVDA260918P00100000",
               "qty": "1", "price": "2.50", "multiplier": "100", "trd_side": "SELL_SHORT",
               "create_time": "2026-09-07 10:30:00"}
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    def process(row, apply=True):
        return _process_payload(row, repo=repo, state_path=tmp_path / "state.json",
                                audit_path=tmp_path / "audit.jsonl", account_mapping={"123": "lx", "456": "lx"},
                                futu_account_ids=["123", "456"], host="127.0.0.1", port=11111,
                                apply_changes=apply, allow_external_lookup=False)
    assert process(payload)["status"] == "applied"
    before = repo.list_trade_events()
    second = {**payload, "deal_id": "second", "order_id": "second-order", **change}
    for apply in (False, True):
        result = process(second, apply)
        assert result["status"] == "unresolved"
        assert error in str(result["diagnostics"])
    assert repo.list_trade_events() == before
    assert len(repo.list_position_lots()) == 1


def _execution_input(deal_id="fill-1", *, namespace="futu.deal", effect="open", option_type="put"):
    return {
        "schema_version": "trade_execution.v1",
        "broker_account_ref": {"broker_id": "futu", "external_account_id": "123", "environment": "REAL",
                               "broker_account_id": "futu:REAL:123", "account_label": "lx"},
        "instrument_ref": {"asset_type": "option", "market": "US", "symbol": "NVDA", "currency": "USD",
                           "option_type": option_type, "strike": "100", "expiration_ymd": "2026-09-18",
                           "multiplier": "100"},
        "external_id_namespace": namespace, "external_execution_id": deal_id,
        "external_order_namespace": "futu.order", "external_order_id": f"order-{deal_id}",
        "side": "sell", "position_effect": effect, "quantity": "1", "price": "2.5", "currency": "USD",
        "occurred_at_utc": "2026-09-07T02:30:00Z",
    }


@pytest.mark.parametrize("source_shape", ["standard", "metadata_only"])
@pytest.mark.parametrize("multiplier", [None, "0", "1.5", "NaN"])
def test_public_ledger_revalidates_standard_multiplier_before_any_write(tmp_path, monkeypatch, source_shape, multiplier):
    from src.application.ledger.api import record_normalized_trade_event
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger import writer_trade_events
    from src.application.trades.normalizer import normalize_trade_deal
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository

    payload = _execution_input()
    payload["instrument_ref"]["multiplier"] = multiplier
    deal = normalize_trade_deal(payload)
    assert deal.execution_input["errors"]
    deal = replace(deal, execution_input={**deal.execution_input, "errors": []},
                   raw_payload=payload if source_shape == "standard" else {})
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    assert repo.list_trade_events() == repo.list_position_lots() == []
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    before_fx = evidence.read_all()

    def unexpected_fx(*_args, **_kwargs):
        raise AssertionError("invalid execution must fail before FX selection")

    monkeypatch.setattr(writer_trade_events, "load_cash_fx_payload", unexpected_fx)
    with pytest.raises(ValueError, match="trade_execution_input_invalid:.*instrument_ref.multiplier"):
        record_normalized_trade_event(repo, deal)
    assert repo.list_trade_events() == repo.list_position_lots() == []
    assert evidence.read_all() == before_fx


def test_public_ledger_revalidates_explicit_standard_raw_without_execution_metadata(tmp_path):
    from src.application.ledger.api import record_normalized_trade_event
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.normalizer import normalize_trade_deal

    payload = _execution_input()
    payload.pop("external_id_namespace")
    deal = replace(normalize_trade_deal(payload), execution_input={})
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    with pytest.raises(ValueError, match="trade_execution_input_invalid:missing:external_id_namespace"):
        record_normalized_trade_event(repo, deal)
    assert repo.list_trade_events() == repo.list_position_lots() == []


@pytest.mark.parametrize("retain_partial_metadata", [False, True])
def test_public_ledger_preserves_legacy_input_without_standard_identity(tmp_path, retain_partial_metadata):
    from src.application.ledger.api import record_normalized_trade_event
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.normalizer import normalize_trade_deal

    payload = {"acc_id": "123", "deal_id": "old-fill", "order_id": "old-order",
               "code": "US.NVDA260918P00100000", "qty": "1", "price": "2.5", "multiplier": "100",
               "trd_side": "SELL_SHORT", "create_time": "2026-09-07 10:30:00"}
    deal = normalize_trade_deal(payload, futu_account_mapping={"123": "lx"}, allow_opend_refresh=False)
    assert deal.execution_input["errors"]
    assert not broker_execution_identity(deal)
    if not retain_partial_metadata:
        deal = replace(deal, execution_input={})
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    result = record_normalized_trade_event(repo, deal)
    assert result.created
    assert result.event_id == "futu:lx:123:old-fill"
    assert len(repo.list_position_lots()) == 1
    assert "execution_input" not in repo.list_trade_events()[0]["raw_payload"]
    assert not record_normalized_trade_event(repo, deal).created


@pytest.mark.parametrize("known_effect,conflicts", [("open", False), ("close", True)])
def test_source_effect_enrichment_checks_applied_allocation_at_ledger_facades(tmp_path, known_effect, conflicts):
    from src.application.ledger.api import record_normalized_trade_event
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.normalizer import normalize_trade_deal
    from src.application.trades.resolver import resolve_trade_deal

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = {**_execution_input(effect=None, option_type="call"), "side": "buy"}
    first = resolve_trade_deal(normalize_trade_deal(payload), repo=repo, state={}, apply_changes=True)
    assert (first.status, first.action) == ("applied", "open")
    before_events, before_lots = repo.list_trade_events(), repo.list_position_lots()
    assert before_events[0]["raw_payload"]["execution_input"]["position_effect"] is None
    enriched = normalize_trade_deal({**payload, "position_effect": known_effect})
    replay = resolve_trade_deal(enriched, repo=repo, state={}, apply_changes=True)
    if conflicts:
        assert (replay.status, replay.reason) == ("unresolved", "trade_execution_applied_association_conflict")
        with pytest.raises(ValueError, match="trade_execution_applied_association_conflict"):
            record_normalized_trade_event(repo, enriched)
    else:
        assert replay.reason == "ledger_recorded"
        assert record_normalized_trade_event(repo, enriched).to_dict()["created"] is False
    assert repo.list_trade_events() == before_events
    assert repo.list_position_lots() == before_lots


def test_intake_isolates_execution_namespaces_and_preserves_legacy_futu_event_ids(tmp_path):
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.auto_intake import _process_payload

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    def process(payload):
        return _process_payload(payload, repo=repo, state_path=tmp_path / "state.json",
                                audit_path=tmp_path / "audit.jsonl", account_mapping={"123": "lx"},
                                futu_account_ids=["123"], host="127.0.0.1", port=11111,
                                source="file", apply_changes=True, allow_external_lookup=False)
    first = process(_execution_input("42"))
    second = process(_execution_input("42", namespace="verified.partition.deal"))
    assert first["status"] == second["status"] == "applied"
    assert first["inbox_id"] != second["inbox_id"]
    before = repo.list_trade_events()
    assert len(before) == 2
    assert "futu:lx:123:42" in {row["event_id"] for row in before}
    assert any(row["event_id"].startswith("execution:v1:") for row in before)
    assert process(_execution_input("42"))["reason"] == "duplicate"
    assert process(_execution_input("42", namespace="verified.partition.deal"))["reason"] == "duplicate"
    assert repo.list_trade_events() == before


@pytest.mark.parametrize("namespace", ["futu.deal", "verified.partition.deal"])
def test_standard_split_close_replay_preserves_event_set_and_rejects_partial_void(tmp_path, namespace):
    from src.application.ledger.api import record_normalized_trade_event, record_trade_event_void
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.normalizer import normalize_trade_deal
    from src.application.trades.resolver import resolve_trade_deal

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    for deal_id in ("opening-1", "opening-2"):
        opened = resolve_trade_deal(normalize_trade_deal(_execution_input(deal_id, namespace=namespace)),
                                    repo=repo, state={}, apply_changes=True)
        assert opened.status == "applied"
    close = normalize_trade_deal({**_execution_input("close-1", namespace=namespace, effect="close"),
                                 "side": "buy", "quantity": "2", "occurred_at_utc": "2026-09-07T03:30:00Z"})
    first = resolve_trade_deal(close, repo=repo, state={}, apply_changes=True)
    assert first.status == "applied"
    assert len(first.operations) == 2
    events, lots = repo.list_trade_events(), repo.list_position_lots()
    recorded = completed_ledger_execution_events(events, close)
    assert len(recorded) == 2
    assert sum(row["contracts"] for row in recorded) == 2
    assert len({row["target_lot_id"] for row in recorded}) == 2
    assert resolve_trade_deal(close, repo=repo, state={}, apply_changes=True).reason == "ledger_recorded"
    assert record_normalized_trade_event(repo, close).to_dict()["created"] is False
    assert repo.list_trade_events() == events
    assert repo.list_position_lots() == lots
    record_trade_event_void(repo, event_id=recorded[0]["event_id"], reason="review invalid allocation")
    after_void = repo.list_trade_events()
    replay = resolve_trade_deal(close, repo=repo, state={}, apply_changes=True)
    assert (replay.status, replay.reason) == ("unresolved", "trade_execution_split_incomplete")
    assert repo.list_trade_events() == after_void


@pytest.mark.parametrize("source_effect", ["close", None])
def test_close_outbox_keeps_namespace_and_original_account_scope_on_replay(tmp_path, source_effect):
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.auto_intake import _process_payload
    from src.application.trades.normalizer import normalize_trade_deal
    from src.application.trades.resolver import resolve_trade_deal

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    closes = []
    for namespace in ("futu.deal", "verified.partition.deal"):
        opened = resolve_trade_deal(normalize_trade_deal(_execution_input("open-42", namespace=namespace)),
                                    repo=repo, state={}, apply_changes=True)
        assert opened.status == "applied"
        close = normalize_trade_deal({**_execution_input("close-42", namespace=namespace, effect=source_effect),
                                     "side": "buy", "occurred_at_utc": "2026-09-07T03:30:00Z"})
        applied = resolve_trade_deal(close, repo=repo, state={}, apply_changes=True)
        assert applied.status == "applied"
        closes.append(close)
    before_events, before_lots = repo.list_trade_events(), repo.list_position_lots()
    notifications = repo.list_trade_lifecycle_notifications()
    assert len(notifications) == 2
    assert {row["case_id"] for row in notifications} == {
        "close:futu:lx:123:close-42",
        f"close:futu:lx:123:{broker_execution_identity(closes[1])}",
    }
    assert all(row["payload"]["total_contracts"] == 1 for row in notifications)
    assert len({row["payload"]["events"][0]["target_lot_id"] for row in notifications}) == 2
    for close in closes:
        renamed = replace(close, internal_account="renamed", execution_input={
            **close.execution_input,
            "broker_account_ref": {**close.execution_input["broker_account_ref"], "account_label": "renamed"},
        })
        replay = _process_payload(
            renamed.execution_input, repo=repo, state_path=tmp_path / "recovery/state.json",
            audit_path=tmp_path / "recovery/audit.jsonl", account_mapping={"123": "renamed"},
            futu_account_ids=["123"], host="localhost", port=11111,
            source="file", apply_changes=True, allow_external_lookup=False,
        )
        assert replay["reason"] == "ledger_recorded"
        assert replay["action"] == "close"
        operation = replay["operations"][0]
        original = next(row for row in notifications if row["payload"]["events"][0]["event_id"] == operation["event_id"])
        assert operation["result"]["notification_outbox_id"] == original["outbox_id"]
        assert operation["result"]["event"]["raw_payload"]["execution_input"]["position_effect"] == source_effect
    assert repo.list_trade_events() == before_events
    assert repo.list_position_lots() == before_lots
    assert repo.list_trade_lifecycle_notifications() == notifications


def test_applied_association_check_tolerates_readless_protocol_repositories():
    from unittest.mock import MagicMock
    from src.application.ledger.api import applied_execution_association_conflicts

    for repo in (None, SimpleNamespace(), MagicMock(), SimpleNamespace(list_trade_events=lambda: None)):
        assert applied_execution_association_conflicts(repo, "execution:v1:test", {"associations": {}}) == []


def test_source_order_enrichment_checks_existing_durable_order_binding(tmp_path):
    from src.application.ledger.api import record_normalized_trade_event
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.normalizer import normalize_trade_deal
    from src.application.trades.resolver import resolve_trade_deal

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = {**_execution_input(), "external_order_id": None, "external_order_namespace": None}
    initial = replace(normalize_trade_deal(payload), order_id="allocated-order")
    assert resolve_trade_deal(initial, repo=repo, state={}, apply_changes=True).status == "applied"
    before = repo.list_trade_events()
    enriched = normalize_trade_deal({**payload, "external_order_id": "other-order", "external_order_namespace": "futu.order"})
    assert resolve_trade_deal(enriched, repo=repo, state={}, apply_changes=True).reason == "trade_execution_applied_association_conflict"
    with pytest.raises(ValueError, match="trade_execution_applied_association_conflict"):
        record_normalized_trade_event(repo, enriched)
    assert repo.list_trade_events() == before


@pytest.mark.parametrize("split,facade", [(False, "resolver"), (True, "resolver"), (True, "writer")])
def test_late_source_order_enriches_whole_execution_without_economic_changes(tmp_path, split, facade):
    from copy import deepcopy
    from src.application.ledger.api import record_normalized_trade_event
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.normalizer import normalize_trade_deal
    from src.application.trades.resolver import resolve_trade_deal
    from src.application.trades.order_fee_sync import recover_order_fee_targets

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    if split:
        for identifier in ("open-a", "open-b"):
            assert resolve_trade_deal(normalize_trade_deal(_execution_input(identifier)), repo=repo,
                                      state={}, apply_changes=True).status == "applied"
    payload = {**_execution_input("late-order", effect="close" if split else "open"),
               "external_order_id": None, "external_order_namespace": None,
               "side": "buy" if split else "sell", "quantity": "2" if split else "1",
               "occurred_at_utc": "2026-09-07T03:30:00Z"}
    assert resolve_trade_deal(normalize_trade_deal(payload), repo=repo, state={}, apply_changes=True).status == "applied"
    before, lots, notifications = repo.list_trade_events(), repo.list_position_lots(), repo.list_trade_lifecycle_notifications()
    enriched = normalize_trade_deal({**payload, "external_order_id": "late-source-order", "external_order_namespace": "futu.order",
                                     "evidence_refs": ["source:history:late-order"]})
    assert resolve_trade_deal(enriched, repo=repo, state={}, apply_changes=False).reason == "ledger_recorded"
    assert repo.list_trade_events() == before
    if facade == "resolver":
        assert resolve_trade_deal(enriched, repo=repo, state={}, apply_changes=True).reason == "ledger_recorded"
    else:
        assert record_normalized_trade_event(repo, enriched).to_dict()["created"] is False
    after = repo.list_trade_events()
    rows = completed_ledger_execution_events(after, enriched)
    assert len(rows) == (2 if split else 1)
    for row in rows:
        raw = row["raw_payload"]
        assert (raw["order_id"], raw["external_order_namespace"]) == ("late-source-order", "futu.order")
        assert (raw["execution_input"]["external_order_id"], raw["execution_input"]["external_order_namespace"]) == ("late-source-order", "futu.order")
        assert raw["execution_order_identity_enrichments"][0]["evidence_refs"] == ["source:history:late-order"]
    reverted = deepcopy(after)
    by_id = {row["event_id"]: row for row in before}
    for row in reverted:
        original = by_id[row["event_id"]]["raw_payload"]
        raw = row["raw_payload"]
        for key in ("order_id", "external_order_namespace", "execution_input", "execution_order_identity_enrichments"):
            if key in original:
                raw[key] = original[key]
            else:
                raw.pop(key, None)
    assert reverted == before
    assert repo.list_position_lots() == lots
    assert repo.list_trade_lifecycle_notifications() == notifications
    targets = recover_order_fee_targets(repo, account="lx", allowed_futu_account_ids=["123"])["targets"]
    assert any(item[-1] == "late-source-order" for item in targets)
    assert resolve_trade_deal(enriched, repo=repo, state={}, apply_changes=True).reason == "ledger_recorded"
    assert repo.list_trade_events() == after


def test_split_order_enrichment_cas_failure_rolls_back_whole_execution(tmp_path, monkeypatch):
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.normalizer import normalize_trade_deal
    from src.application.trades.resolver import resolve_trade_deal

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    for identifier in ("open-a", "open-b"):
        assert resolve_trade_deal(normalize_trade_deal(_execution_input(identifier)), repo=repo,
                                  state={}, apply_changes=True).status == "applied"
    payload = {**_execution_input("close", effect="close"), "side": "buy", "quantity": "2",
               "external_order_id": None, "external_order_namespace": None, "occurred_at_utc": "2026-09-07T03:30:00Z"}
    assert resolve_trade_deal(normalize_trade_deal(payload), repo=repo, state={}, apply_changes=True).status == "applied"
    before, lots, notifications = repo.list_trade_events(), repo.list_position_lots(), repo.list_trade_lifecycle_notifications()
    original_cas = repo.compare_and_swap_trade_event_order_identity_json
    calls = []
    def fail_second(**kwargs):
        calls.append(kwargs["event_id"])
        return original_cas(**kwargs) if len(calls) == 1 else False
    monkeypatch.setattr(repo, "compare_and_swap_trade_event_order_identity_json", fail_second)
    result = resolve_trade_deal(normalize_trade_deal({**payload, "external_order_id": "late", "external_order_namespace": "futu.order"}),
                                repo=repo, state={}, apply_changes=True)
    assert result.reason == "trade_execution_order_binding_cas_conflict"
    assert len(calls) == 2
    assert repo.list_trade_events() == before
    assert repo.list_position_lots() == lots
    assert repo.list_trade_lifecycle_notifications() == notifications


def _direct_split_close_fixture(tmp_path, *, namespace="futu.deal", standard=True):
    from src.application.ledger.api import record_normalized_trade_event
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.normalizer import normalize_trade_deal

    def normalize(payload):
        deal = normalize_trade_deal(payload)
        return deal if standard else replace(deal, execution_input={}, raw_payload={})

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    for identifier in ("direct-open-a", "direct-open-b"):
        assert record_normalized_trade_event(
            repo, normalize(_execution_input(identifier, namespace=namespace)),
        ).created
    close = normalize({
        **_execution_input("direct-close", namespace=namespace, effect="close"),
        "side": "buy", "quantity": "2", "occurred_at_utc": "2026-09-07T03:30:00Z",
    })
    return repo, close


def test_public_atomic_close_rejects_invalid_execution_without_changing_lots_or_fx(tmp_path, monkeypatch):
    from src.application.ledger.api import (
        record_broker_trade_close, record_normalized_trade_event, resolve_broker_trade_close_targets,
    )
    from src.application.ledger import writer_trade_events
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository

    repo, close = _direct_split_close_fixture(tmp_path)
    execution = {**close.execution_input, "instrument_ref": {**close.execution_input["instrument_ref"], "multiplier": None}, "errors": []}
    close = replace(close, execution_input=execution, multiplier=None)
    resolution = resolve_broker_trade_close_targets(repo, deal=close)
    assert len(resolution.matches) == 2
    before_events, before_lots = repo.list_trade_events(), repo.list_position_lots()
    before_outbox = repo.list_trade_lifecycle_notifications()
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    before_fx = evidence.read_all()

    def unexpected_fx(*_args, **_kwargs):
        raise AssertionError("invalid execution must fail before FX selection")

    monkeypatch.setattr(writer_trade_events, "load_cash_fx_payload", unexpected_fx)
    with pytest.raises(ValueError, match="trade_execution_input_invalid:missing:instrument_ref.multiplier"):
        record_broker_trade_close(repo, matches=list(resolution.matches), deal=close,
                                  persist_trade_event_fn=record_normalized_trade_event,
                                  close_target_resolution=resolution)
    assert repo.list_trade_events() == before_events
    assert repo.list_position_lots() == before_lots
    assert repo.list_trade_lifecycle_notifications() == before_outbox
    assert evidence.read_all() == before_fx


@pytest.mark.parametrize("namespace", ["futu.deal", "verified.partition.deal"])
def test_direct_ledger_close_splits_once_and_rejects_partially_voided_replay(tmp_path, namespace):
    from src.application.ledger.api import record_normalized_trade_event, record_trade_event_void
    from src.application.trades.resolver import resolve_trade_deal

    repo, close = _direct_split_close_fixture(tmp_path, namespace=namespace)
    target_ids = {row["record_id"] for row in repo.list_position_lots()}
    assert record_normalized_trade_event(repo, close).created
    events, lots = repo.list_trade_events(), repo.list_position_lots()
    closes = sorted(
        completed_ledger_execution_events(events, close),
        key=lambda row: row["raw_payload"]["broker_deal_completion"]["split_index"],
    )
    assert len(closes) == 2
    assert {row["target_lot_id"] for row in closes} == target_ids
    key = broker_external_event_key(close)
    assert closes[0]["event_id"] == key
    assert closes[1]["event_id"] == f"{key}:target:{closes[1]['target_lot_id']}"
    for index, event in enumerate(closes, start=1):
        assert event["contracts"] == 1
        assert event["raw_payload"]["fee_order_group_id"] == key
        assert event["raw_payload"]["broker_deal_completion"] == {
            "source_deal_id": "direct-close", "expected_contracts": 2,
            "split_count": 2, "split_index": index, "allocated_contracts": 1,
        }
        assert event["raw_payload"]["execution_input"] == close.execution_input
        assert event["raw_payload"]["cash_conversions"]
    assert record_normalized_trade_event(repo, close).created is False
    assert resolve_trade_deal(close, repo=repo, state={}, apply_changes=True).reason == "ledger_recorded"
    assert repo.list_trade_events() == events
    assert repo.list_position_lots() == lots

    record_trade_event_void(repo, event_id=closes[0]["event_id"], reason="invalid split allocation")
    voided_events, voided_lots = repo.list_trade_events(), repo.list_position_lots()
    with pytest.raises(ValueError, match="trade_execution_split_incomplete"):
        record_normalized_trade_event(repo, close)
    assert resolve_trade_deal(close, repo=repo, state={}, apply_changes=True).reason == "trade_execution_split_incomplete"
    assert repo.list_trade_events() == voided_events
    assert repo.list_position_lots() == voided_lots


@pytest.mark.parametrize("namespace", ["futu.deal", "verified.partition.deal"])
def test_direct_ledger_split_failure_rolls_back_all_events_and_can_retry(tmp_path, monkeypatch, namespace):
    from src.application.ledger.api import record_normalized_trade_event

    repo, close = _direct_split_close_fixture(tmp_path, namespace=namespace)
    events, lots = repo.list_trade_events(), repo.list_position_lots()
    notifications = repo.list_trade_lifecycle_notifications()
    original = repo.upsert_trade_event
    inserted = []

    def fail_after_second_split(event, *, conn=None):
        created = original(event, conn=conn)
        inserted.append(event.event_id)
        if len(inserted) == 2:
            raise RuntimeError("injected failure after second close insert")
        return created

    with monkeypatch.context() as patch:
        patch.setattr(repo, "upsert_trade_event", fail_after_second_split)
        with pytest.raises(RuntimeError, match="injected failure"):
            record_normalized_trade_event(repo, close)
    assert len(inserted) == 2
    assert repo.list_trade_events() == events
    assert repo.list_position_lots() == lots
    assert repo.list_trade_lifecycle_notifications() == notifications
    assert record_normalized_trade_event(repo, close).created
    assert len(completed_ledger_execution_events(repo.list_trade_events(), close)) == 2
    assert record_normalized_trade_event(repo, close).created is False


def test_legacy_direct_ledger_close_keeps_existing_split_metadata_shape(tmp_path):
    from src.application.ledger.api import record_normalized_trade_event

    repo, close = _direct_split_close_fixture(tmp_path, standard=False)
    assert record_normalized_trade_event(repo, close).created
    closes = [event for event in repo.list_trade_events() if event["event_type"] == "close"]
    assert len(closes) == 2
    key = broker_external_event_key(close)
    assert {row["event_id"] for row in closes} == {
        key,
        f"{key}:target:{max(row['target_lot_id'] for row in closes)}",
    }
    for event in closes:
        assert event["contracts"] == 1
        assert event["raw_payload"]["fee_order_group_id"] == key
        assert event["raw_payload"]["close_target_resolution"]["contracts_to_close"] == 2
        assert "broker_deal_completion" not in event["raw_payload"]
        assert "execution_input" not in event["raw_payload"]


def _proven_legacy_execution_fixture(tmp_path, *, split=False, order_known=True, source_changes=None):
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.normalizer import normalize_trade_deal
    from src.application.trades.resolver import resolve_trade_deal

    payload = {
        "acc_id": "123", "broker_account_id": "futu:REAL:123", "environment": "REAL",
        "external_id_namespace": "futu.deal", "external_order_namespace": "futu.order",
        "deal_id": "legacy-fill", "order_id": "legacy-order", "code": "US.NVDA260918P00100000",
        "qty": "1", "price": "2.50", "multiplier": "100", "trd_side": "SELL_SHORT",
        "create_time": "2026-09-07 10:30:00",
    }
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")

    def normalize(raw):
        return normalize_trade_deal(raw, futu_account_mapping={"123": "lx"}, allow_opend_refresh=False)

    def write_legacy(raw):
        result = resolve_trade_deal(replace(normalize(raw), execution_input={}), repo=repo,
                                    state={}, apply_changes=True)
        assert result.status == "applied"

    if split:
        for identifier in ("old-open-a", "old-open-b"):
            write_legacy({**payload, "deal_id": identifier, "order_id": f"order-{identifier}"})
        payload = {**payload, "qty": "2", "price": "1", "trd_side": "BUY_BACK",
                   "create_time": "2026-09-07 11:30:00"}
    old_payload = {**payload, **(source_changes or {})}
    if not order_known:
        old_payload.pop("order_id")
        old_payload.pop("external_order_namespace")
    write_legacy(old_payload)
    return repo, payload, normalize(payload)


@pytest.mark.parametrize("split", [False, True])
def test_proven_legacy_order_replay_preserves_original_group_at_public_intake(tmp_path, monkeypatch, split):
    from src.application.ledger import api
    from src.application.trades.auto_intake import _process_payload
    from src.application.trades.inbox import read_trade_payload
    from src.application.trades.resolver import resolve_trade_deal

    repo, payload, deal = _proven_legacy_execution_fixture(tmp_path, split=split)
    before, lots = repo.list_trade_events(), repo.list_position_lots()
    notifications = repo.list_trade_lifecycle_notifications()
    recorded = completed_ledger_execution_events(before, deal)
    assert len(recorded) == (2 if split else 1)
    assert all("execution_input" not in row["raw_payload"] for row in recorded)
    assert all(row["raw_payload"]["cash_conversions"] for row in recorded)
    assert len(notifications) == int(split)

    def forbid_enrichment(*args, **kwargs):
        raise AssertionError("proven legacy order must not invoke a metadata writer")
    monkeypatch.setattr(api, "reconcile_normalized_execution_order_identity", forbid_enrichment)
    expected_ids = {row["event_id"] for row in recorded}
    for apply in (False, True):
        replay = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=apply)
        assert replay.reason == "ledger_recorded"
        assert {operation.event_id for operation in replay.operations} == expected_ids
    result = _process_payload(
        payload, repo=repo, state_path=tmp_path / "state.json", audit_path=tmp_path / "audit.jsonl",
        account_mapping={"123": "lx"}, futu_account_ids=["123"], host="localhost", port=11111,
        source="file", apply_changes=True, allow_external_lookup=False,
    )
    assert result["reason"] == "ledger_recorded"
    assert {operation["event_id"] for operation in result["operations"]} == expected_ids
    inbox = read_trade_payload(tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3", inbox_id=result["inbox_id"])
    assert inbox["status"] == "handled"
    if split:
        assert all(operation["result"]["notification_outbox_id"] == notifications[0]["outbox_id"]
                   for operation in result["operations"])
    assert repo.list_trade_events() == before
    assert repo.list_position_lots() == lots
    assert repo.list_trade_lifecycle_notifications() == notifications


@pytest.mark.parametrize("split", [False, True])
def test_legacy_missing_order_requires_review_in_preview_apply_and_core(tmp_path, split):
    from src.application.trades.auto_intake import _process_payload
    from src.application.trades.resolver import resolve_trade_deal

    repo, payload, deal = _proven_legacy_execution_fixture(tmp_path, split=split, order_known=False)
    before, lots = repo.list_trade_events(), repo.list_position_lots()
    notifications = repo.list_trade_lifecycle_notifications()
    assert len(completed_ledger_execution_events(before, deal)) == (2 if split else 1)
    for apply in (False, True):
        result = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=apply)
        assert (result.status, result.reason) == ("unresolved", "legacy_execution_evidence_required")
        assert result.diagnostics["errors"] == ["legacy_order_binding_not_proven"]
        assert result.operations == []
    result = _process_payload(
        payload, repo=repo, state_path=tmp_path / "state.json", audit_path=tmp_path / "audit.jsonl",
        account_mapping={"123": "lx"}, futu_account_ids=["123"], host="localhost", port=11111,
        source="file", apply_changes=True, allow_external_lookup=False,
    )
    assert (result["status"], result["reason"]) == ("unresolved", "legacy_execution_evidence_required")
    assert repo.list_trade_events() == before
    assert repo.list_position_lots() == lots
    assert repo.list_trade_lifecycle_notifications() == notifications


@pytest.mark.parametrize("source_changes,reason", [
    ({"environment": None}, "legacy_execution_evidence_required"),
    ({"broker_account_id": None}, "legacy_execution_evidence_required"),
    ({"price": "3"}, "trade_execution_economic_conflict"),
    ({"order_id": "other-order"}, "trade_execution_applied_association_conflict"),
])
def test_legacy_order_readback_keeps_scope_economics_and_association_guards(tmp_path, source_changes, reason):
    from src.application.trades.resolver import resolve_trade_deal

    repo, _payload, deal = _proven_legacy_execution_fixture(tmp_path, source_changes=source_changes)
    before, lots = repo.list_trade_events(), repo.list_position_lots()
    for apply in (False, True):
        replay = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=apply)
        assert (replay.status, replay.reason) == ("unresolved", reason)
        assert replay.operations == []
    assert repo.list_trade_events() == before
    assert repo.list_position_lots() == lots


def test_legacy_order_readback_rejects_partial_split_void(tmp_path):
    from src.application.ledger.api import record_trade_event_void
    from src.application.trades.resolver import resolve_trade_deal

    repo, _payload, deal = _proven_legacy_execution_fixture(tmp_path, split=True)
    close = completed_ledger_execution_events(repo.list_trade_events(), deal)[0]
    record_trade_event_void(repo, event_id=close["event_id"], reason="invalid split allocation")
    before, lots = repo.list_trade_events(), repo.list_position_lots()
    for apply in (False, True):
        replay = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=apply)
        assert (replay.status, replay.reason) == ("unresolved", "trade_execution_split_incomplete")
    assert repo.list_trade_events() == before
    assert repo.list_position_lots() == lots


def test_canonical_order_enrichment_empty_readback_requires_review(tmp_path, monkeypatch):
    from src.application.ledger import api
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.normalizer import normalize_trade_deal
    from src.application.trades.resolver import resolve_trade_deal

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    deal = normalize_trade_deal(_execution_input())
    assert resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True).status == "applied"
    before, lots = repo.list_trade_events(), repo.list_position_lots()
    monkeypatch.setattr(api, "reconcile_normalized_execution_order_identity", lambda *args, **kwargs: [])
    assert resolve_trade_deal(deal, repo=repo, state={}, apply_changes=False).reason == "ledger_recorded"
    replay = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
    assert (replay.status, replay.reason) == ("unresolved", "trade_execution_order_binding_event_missing")
    assert repo.list_trade_events() == before
    assert repo.list_position_lots() == lots
