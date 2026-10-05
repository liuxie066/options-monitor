from __future__ import annotations

from dataclasses import replace

import pytest

from domain.domain.ledger.identity import position_key_for
from domain.domain.option_lifecycle import expiration_observation_start_ms
from src.application.ledger.lifecycle_migration import (
    apply_lifecycle_migration_manifest,
    build_lifecycle_migration_inventory,
    select_lifecycle_migration_targets,
)
from src.application.ledger.notification_outbox import canonical_payload_hash
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.application.trades.close_reason_evidence import build_lifecycle_timing_policy
from tests.test_lifecycle_redesign_contracts import EXPIRATION_YMD, _open_event


def _legacy_case(repo, index, multiplier):
    lot_id = f"lot-{index}"
    case_id = f"legacy-case-{index}"
    event = replace(
        _open_event(),
        event_id=f"open-{index}",
        lot_id=lot_id,
        multiplier=multiplier,
        raw_payload={"fields": {**_open_event().raw_payload["fields"], "multiplier": multiplier}},
    )
    persist_trade_event_object(repo, event)
    observed_at = expiration_observation_start_ms(EXPIRATION_YMD, "US")
    assert repo.upsert_trade_lifecycle_case(
        {
            "schema_version": "lifecycle_case.v1",
            "case_id": case_id,
            "case_key": case_id,
            "account": "lx",
            "broker": "futu",
            "contract_key": position_key_for(event.contract_key, "short"),
            "position_side": "short",
            "expiration_ymd": EXPIRATION_YMD,
            "market": "US",
            "symbol": "NVDA",
            "option_type": "put",
            "strike": 100,
            "currency": "USD",
            "multiplier": multiplier,
            "target_contracts_by_lot": {lot_id: 1},
            "status": "waiting_settlement_evidence",
        }
    )
    assert repo.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": f"legacy-anchor-{index}",
            "case_id": case_id,
            "source_type": "futu_broker_deal",
            "source_event_id": f"futu:lx:1001:legacy-option-close-{index}",
            "evidence_type": "option_zero_price_close",
            "account": "lx",
            "futu_account_id": "1001",
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "strike": 100,
            "expiration_ymd": EXPIRATION_YMD,
            "contracts": 1,
            "price": 0,
            "event_time_ms": observed_at,
        }
    )
    assert repo.insert_trade_lifecycle_timing_policy_once(
        build_lifecycle_timing_policy(
            case_id=case_id,
            market="US",
            expiration_ymd=EXPIRATION_YMD,
            contract_metadata={
                "settlement_style": "physical",
                "underlying_security_type": "equity",
                "last_trade_cutoff_ms": observed_at - 1,
                "last_trade_cutoff_source": "instrument_policy_registry",
            },
            trading_days=[{"date": date, "type": "TRADING"} for date in ("2026-08-21", "2026-08-24", "2026-08-25")],
            calendar_source="test_calendar",
            calendar_observed_at_ms=observed_at,
        )
    )
    return case_id


def _batch(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    case_ids = [_legacy_case(repo, 1, 500), _legacy_case(repo, 2, 1000)]
    inventory = build_lifecycle_migration_inventory(repo)
    manifest = select_lifecycle_migration_targets(
        inventory, target_keys=[f"lifecycle:{case_id}" for case_id in case_ids]
    )
    assert all(row["mapping_status"] == "exact" for row in manifest["rows"])
    return repo, manifest


def _dump(repo):
    with repo._optional_conn(None) as conn:
        return tuple(conn.iterdump())


def _rehash(manifest):
    manifest["manifest_hash"] = canonical_payload_hash(
        {"schema_version": manifest["schema_version"], "rows": manifest["rows"]}
    )


@pytest.mark.parametrize("invalid", [None, "", True, 0, -1, 100.5, "100.00000000000000001", "NaN", "Infinity"])
@pytest.mark.parametrize("apply_changes", [False, True])
def test_second_invalid_multiplier_rejects_whole_batch(tmp_path, invalid, apply_changes):
    repo, _ = _batch(tmp_path)
    case = repo.get_trade_lifecycle_case("legacy-case-2")
    case["multiplier"] = invalid
    repo.upsert_trade_lifecycle_case(case)
    inventory = build_lifecycle_migration_inventory(repo)
    row = next(row for row in inventory["rows"] if row["case_id"] == "legacy-case-2")
    assert row["mapping_status"] == "needs_review"
    assert any("multiplier" in reason for reason in row["review_reason_codes"])
    manifest = select_lifecycle_migration_targets(
        inventory, target_keys=[row["target_key"] for row in inventory["rows"]]
    )
    before = _dump(repo)
    with pytest.raises(ValueError, match="migration_needs_review"):
        apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=apply_changes)
    assert _dump(repo) == before


@pytest.mark.parametrize("apply_changes", [False, True])
def test_second_source_drift_rejects_batch_after_preview(tmp_path, apply_changes):
    repo, manifest = _batch(tmp_path)
    apply_lifecycle_migration_manifest(repo, manifest=manifest)
    case = repo.get_trade_lifecycle_case("legacy-case-2")
    case["multiplier"] = 500
    repo.upsert_trade_lifecycle_case(case)
    before = _dump(repo)
    with pytest.raises(ValueError, match="source drift"):
        apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=apply_changes)
    assert _dump(repo) == before


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_late_second_row_failure_rolls_back_every_first_row_effect(tmp_path, monkeypatch, failure):
    repo, manifest = _batch(tmp_path)
    before = _dump(repo)
    original = repo.insert_trade_lifecycle_migration_receipt_once
    calls = []

    def fail_second(receipt, *, conn=None):
        calls.append(receipt["target_key"])
        if len(calls) == 2:
            raise failure("injected late write failure")
        return original(receipt, conn=conn)

    monkeypatch.setattr(repo, "insert_trade_lifecycle_migration_receipt_once", fail_second)
    with pytest.raises(failure, match="injected late write failure"):
        apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=True)
    assert len(calls) == 2
    assert _dump(repo) == before


def test_existing_receipt_survives_new_row_failure(tmp_path):
    repo, manifest = _batch(tmp_path)
    first = select_lifecycle_migration_targets(manifest, target_keys=["lifecycle:legacy-case-1"])
    assert apply_lifecycle_migration_manifest(repo, manifest=first, apply_changes=True)["applied_count"] == 1
    case = repo.get_trade_lifecycle_case("legacy-case-2")
    case["multiplier"] = None
    repo.upsert_trade_lifecycle_case(case)
    before = _dump(repo)
    with pytest.raises(ValueError, match="source drift"):
        apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=True)
    assert _dump(repo) == before
    retry = apply_lifecycle_migration_manifest(repo, manifest=first, apply_changes=True)
    assert retry["existing_count"] == 1
    assert retry["applied_count"] == 0
    assert _dump(repo) == before


def test_successful_batch_preserves_actual_multipliers_and_retries_noop(tmp_path):
    repo, manifest = _batch(tmp_path)
    before = _dump(repo)
    preview = apply_lifecycle_migration_manifest(repo, manifest=manifest)
    assert len(preview["would_apply_target_keys"]) == 2
    assert _dump(repo) == before
    assert apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=True)["applied_count"] == 2
    migrated = [case for case in repo.list_trade_lifecycle_cases() if case["schema_version"] == "lifecycle_case.v2"]
    assert sorted(case["multiplier"] for case in migrated) == [500, 1000]
    after = _dump(repo)
    assert apply_lifecycle_migration_manifest(repo, manifest=manifest)["would_apply_target_keys"] == []
    result = apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=True)
    assert (result["applied_count"], result["existing_count"]) == (0, 2)
    assert _dump(repo) == after


@pytest.mark.parametrize("apply_changes", [False, True])
def test_duplicate_target_rejected_without_writes(tmp_path, apply_changes):
    repo, manifest = _batch(tmp_path)
    manifest["rows"].append(dict(manifest["rows"][0]))
    _rehash(manifest)
    before = _dump(repo)
    with pytest.raises(ValueError, match="duplicated"):
        apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=apply_changes)
    assert _dump(repo) == before


@pytest.mark.parametrize("apply_changes", [False, True])
def test_rehashed_invalid_canonical_plan_is_rejected(tmp_path, apply_changes):
    repo, manifest = _batch(tmp_path)
    manifest["rows"][1]["legacy_upgrade"]["canonical_case"]["multiplier"] = True
    _rehash(manifest)
    before = _dump(repo)
    with pytest.raises(ValueError, match="multiplier"):
        apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=apply_changes)
    assert _dump(repo) == before


@pytest.mark.parametrize("apply_changes", [False, True])
def test_existing_notification_conflict_fails_in_preview_and_apply(tmp_path, apply_changes):
    from src.application.ledger.notification_outbox import build_notification_intent

    repo, manifest = _batch(tmp_path)
    second = manifest["rows"][1]
    canonical_id = second["legacy_upgrade"]["canonical_case"]["case_id"]
    repo.insert_trade_lifecycle_notification_once(
        build_notification_intent(
            case_id=canonical_id,
            transition_type="option_leg_closed",
            resolution_revision=1,
            transition_key=f"lifecycle:{canonical_id}:option_leg_closed",
            state_fingerprint="existing-other-intent",
            payload={"case_id": canonical_id},
        )
    )
    # The canonical case does not exist yet, so this outbox does not alter the
    # legacy row inventory hash; its uniqueness constraint still gates the batch.
    current = build_lifecycle_migration_inventory(repo)
    row = next(item for item in current["rows"] if item["target_key"] == second["target_key"])
    assert row["mapping_status"] == "needs_review"
    assert "notification_slot_occupied" in row["review_reason_codes"]
    before = _dump(repo)
    with pytest.raises(ValueError, match="migration_needs_review"):
        apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=apply_changes)
    assert _dump(repo) == before


def test_existing_receipt_rejects_changed_row_hash(tmp_path):
    repo, manifest = _batch(tmp_path)
    apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=True)
    before = _dump(repo)
    manifest["rows"][0]["suppress_option_leg_closed"] = False
    _rehash(manifest)
    with pytest.raises(ValueError, match="receipt row conflict"):
        apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=True)
    assert _dump(repo) == before


@pytest.mark.parametrize("apply_changes", [False, True])
@pytest.mark.parametrize("selected", [False, True])
def test_final_notification_request_is_rejected_before_writes(
    tmp_path, apply_changes, selected
):
    repo, manifest = _batch(tmp_path)
    if not selected:
        manifest = select_lifecycle_migration_targets(manifest, target_keys=[])
    manifest["rows"][0]["seed_final_intent"] = True
    _rehash(manifest)
    before = _dump(repo)
    with pytest.raises(ValueError, match="cannot seed final notifications"):
        apply_lifecycle_migration_manifest(
            repo, manifest=manifest, apply_changes=apply_changes
        )
    assert _dump(repo) == before


def test_legacy_false_manifest_replays_without_final_notification(tmp_path):
    repo, manifest = _batch(tmp_path)
    assert all("seed_final_intent" not in row for row in manifest["rows"])
    for row in manifest["rows"]:
        row["seed_final_intent"] = False
    _rehash(manifest)

    assert apply_lifecycle_migration_manifest(repo, manifest=manifest)[
        "status"
    ] == "dry_run"
    assert apply_lifecycle_migration_manifest(
        repo, manifest=manifest, apply_changes=True
    )["applied_count"] == 2
    notifications = repo.list_trade_lifecycle_notifications()
    assert notifications
    assert all(item["status"] == "suppressed" for item in notifications)
    assert all(
        item["transition_type"] != "resolution_confirmed"
        for item in notifications
    )
    before = _dump(repo)
    assert apply_lifecycle_migration_manifest(
        repo, manifest=manifest, apply_changes=True
    )["status"] == "noop"
    assert _dump(repo) == before


@pytest.mark.parametrize("conflict", ["batch", "existing"])
@pytest.mark.parametrize("apply_changes", [False, True])
def test_conflicting_source_owner_blocks_entire_selected_batch(tmp_path, conflict, apply_changes):
    from src.application.ledger.source_consumption import build_source_consumption_claim

    repo, _ = _batch(tmp_path)
    source_key = "futu:lx:1001:legacy-option-close-1"
    if conflict == "batch":
        repo.insert_trade_lifecycle_evidence_once(
            {
                "evidence_id": "conflicting-stock-leg",
                "case_id": "legacy-case-2",
                "source_type": "futu_broker_deal",
                "source_event_id": source_key,
                "evidence_type": "stock_settlement_leg",
                "account": "lx",
                "futu_account_id": "1001",
                "symbol": "NVDA",
                "shares": 1000,
                "price": 100,
                "event_time_ms": 1_780_000_000_000,
            }
        )
        reason = "source_claim_owner_ambiguous"
    else:
        repo.insert_trade_lifecycle_source_consumption_once(
            build_source_consumption_claim(
                source_key=source_key,
                case_id="legacy-case-2",
                owner_evidence_id="legacy-anchor-2",
                source_role="option_anchor",
                economic_payload={"account": "lx", "futu_account_id": "1001"},
            )
        )
        reason = "source_claim_existing_owner_conflict"
    inventory = build_lifecycle_migration_inventory(repo)
    first = next(row for row in inventory["rows"] if row["case_id"] == "legacy-case-1")
    assert reason in first["review_reason_codes"]
    manifest = select_lifecycle_migration_targets(
        inventory, target_keys=[row["target_key"] for row in inventory["rows"]]
    )
    before = _dump(repo)
    with pytest.raises(ValueError, match="migration_needs_review"):
        apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=apply_changes)
    assert _dump(repo) == before


def test_existing_receipt_and_new_valid_row_apply_once(tmp_path):
    repo, manifest = _batch(tmp_path)
    first = select_lifecycle_migration_targets(manifest, target_keys=["lifecycle:legacy-case-1"])
    assert apply_lifecycle_migration_manifest(repo, manifest=first, apply_changes=True)["applied_count"] == 1
    preview = apply_lifecycle_migration_manifest(repo, manifest=manifest)
    assert preview["existing_count"] == 1
    assert preview["would_apply_target_keys"] == ["lifecycle:legacy-case-2"]
    result = apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=True)
    assert result["status"] == "applied"
    assert (result["applied_count"], result["existing_count"]) == (1, 1)
    assert len(repo.list_trade_lifecycle_migration_receipts()) == 2


@pytest.mark.parametrize("invalid", [True, "100.00000000000000000000000000001"])
@pytest.mark.parametrize("owner", ["event", "lot"])
@pytest.mark.parametrize("apply_changes", [False, True])
def test_explicit_mapping_rejects_raw_invalid_event_and_lot_multipliers(tmp_path, invalid, owner, apply_changes):
    import json
    from tests.ledger_sqlite_test_support import connect_ledger_fixture
    from tests.test_lifecycle_redesign_contracts import _legacy_terminal_mapping_fixture

    repo, case_id, mapping, event_id = _legacy_terminal_mapping_fixture(tmp_path)
    with connect_ledger_fixture(repo.db_path) as conn:
        if owner == "event":
            payload = json.loads(
                conn.execute("SELECT event_json FROM trade_events WHERE event_id=?", (event_id,)).fetchone()[0]
            )
            payload["multiplier"] = invalid
            conn.execute("UPDATE trade_events SET event_json=? WHERE event_id=?", (json.dumps(payload), event_id))
        else:
            fields = json.loads(
                conn.execute("SELECT fields_json FROM position_lots WHERE lot_id='lot-1'").fetchone()[0]
            )
            fields["multiplier"] = invalid
            conn.execute("UPDATE position_lots SET fields_json=? WHERE lot_id='lot-1'", (json.dumps(fields),))
    inventory = build_lifecycle_migration_inventory(repo, explicit_mapping=mapping)
    selected = next(row for row in inventory["rows"] if row["target_key"] == f"lifecycle:{case_id}")
    assert selected["mapping_status"] == "needs_review"
    manifest = select_lifecycle_migration_targets(inventory, target_keys=[selected["target_key"]])
    before = _dump(repo)
    with pytest.raises(ValueError, match="migration_needs_review"):
        apply_lifecycle_migration_manifest(repo, manifest=manifest, apply_changes=apply_changes)
    assert _dump(repo) == before


@pytest.mark.parametrize("apply_changes", [False, True])
def test_empty_selection_validates_hash_and_rejects_apply_without_sqlite(apply_changes):
    manifest = {"schema_version": "lifecycle_cutover_manifest.v1", "rows": []}
    _rehash(manifest)
    if apply_changes:
        with pytest.raises(ValueError, match="no selected rows"):
            apply_lifecycle_migration_manifest(None, manifest=manifest, apply_changes=True)
    else:
        result = apply_lifecycle_migration_manifest(None, manifest=manifest, apply_changes=False)
        assert result == {
            "schema_version": "lifecycle_migration_apply_result.v1",
            "status": "dry_run",
            "manifest_hash": manifest["manifest_hash"],
            "selected_count": 0,
            "applied_count": 0,
            "existing_count": 0,
            "would_apply_target_keys": [],
        }
    manifest["manifest_hash"] = "invalid"
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        apply_lifecycle_migration_manifest(None, manifest=manifest, apply_changes=apply_changes)


@pytest.mark.parametrize("apply_changes", [False, True])
def test_nonempty_selection_still_requires_sqlite(tmp_path, apply_changes):
    _, manifest = _batch(tmp_path)
    with pytest.raises(TypeError, match="repository interface"):
        apply_lifecycle_migration_manifest(None, manifest=manifest, apply_changes=apply_changes)



def test_notification_delivery_attempt_does_not_change_migration_source_hash(tmp_path):
    from src.application.ledger.notification_outbox import build_notification_intent

    repo, _ = _batch(tmp_path)
    intent = build_notification_intent(
        case_id="legacy-case-1", transition_type="resolution_confirmed",
        resolution_revision=1, transition_key="lifecycle:legacy-case-1:resolution_confirmed",
        state_fingerprint="frozen-intent", payload={"case_id": "legacy-case-1"},
    )
    assert repo.insert_trade_lifecycle_notification_once(intent)
    before = build_lifecycle_migration_inventory(repo)
    first = next(row for row in before["rows"] if row["target_key"] == "lifecycle:legacy-case-1")
    assert repo.compare_and_set_trade_lifecycle_notification(
        outbox_id=intent["outbox_id"], expected_status="pending", new_status="confirmed",
        fields={"confirmed_at_ms": int(repo.get_trade_lifecycle_notification(intent["outbox_id"])["created_at_ms"])},
    )
    after = build_lifecycle_migration_inventory(repo)
    updated = next(row for row in after["rows"] if row["target_key"] == first["target_key"])
    assert updated["inventory_state_hash"] == first["inventory_state_hash"]
    assert updated["mapping_status"] == first["mapping_status"] == "exact"


def test_migration_owned_notification_slot_has_distinct_review_reason(tmp_path):
    from src.application.ledger.notification_outbox import build_notification_intent

    repo, manifest = _batch(tmp_path)
    second = manifest["rows"][1]
    canonical_id = second["legacy_upgrade"]["canonical_case"]["case_id"]
    assert repo.insert_trade_lifecycle_notification_once(build_notification_intent(
        case_id=canonical_id, transition_type="option_leg_closed",
        resolution_revision=1, transition_key=f"lifecycle:{canonical_id}:option_leg_closed",
        state_fingerprint="previous-migration",
        payload={"schema_version": "migration_notification_suppression.v1",
                 "migration_target": "lifecycle:other-case"},
    ))
    row = next(item for item in build_lifecycle_migration_inventory(repo)["rows"]
               if item["target_key"] == second["target_key"])
    assert row["mapping_status"] == "needs_review"
    assert "notification_slot_owned_by_migration" in row["review_reason_codes"]
    assert "notification_slot_occupied" not in row["review_reason_codes"]


def test_normal_close_inventory_uses_writer_execution_namespace():
    from src.application.ledger.external_event_key import futu_compatibility_source_key
    from src.application.ledger.lifecycle_migration import _normal_close_inventory_rows

    execution_input = {
        "external_id_namespace": "manual.import",
        "external_execution_id": "deal-1",
        "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001",
                               "environment": "REAL"},
    }
    event = {
        "event_id": "close-1", "event_type": "close", "asset_type": "stock",
        "account": "lx", "raw_payload": {"futu_account_id": "1001",
            "source_deal_id": "deal-1", "execution_input": execution_input},
    }
    rows = _normal_close_inventory_rows([event], [], void_event_ids=set())
    expected = futu_compatibility_source_key(account="lx", futu_account_id="1001",
        source_deal_id="deal-1", execution_input=execution_input)
    assert len(rows) == 1
    assert rows[0]["broker_deal_key"] == expected
    assert expected != "futu:lx:1001:deal-1"
