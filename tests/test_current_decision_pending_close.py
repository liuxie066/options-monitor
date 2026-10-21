from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.application.ledger import api
from src.application.ledger.current_decision_payload import read_lifecycle_case_decision_fact
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.lifecycle import resolve_lifecycle_trade_deal
from src.application.trades.lifecycle_reconciliation import lifecycle_case_read_model
from src.application.trades.normalizer import normalize_trade_deal


NOW = 1_787_342_400_000
ACCOUNT = "synthetic"


def _open(repo: SQLiteOptionPositionsRepository, *, account: str = ACCOUNT,
          symbol: str = "NVDA", contracts: int = 1) -> str:
    before = {row["record_id"] for row in repo.list_position_lots()}
    api.record_manual_position_open(
        repo, broker="futu", account=account, symbol=symbol, option_type="put", side="short",
        contracts=contracts, currency="USD", strike=100, multiplier=100,
        expiration_ymd="2026-08-21", premium_per_share=2, opened_at_ms=1_787_200_000_000,
        request_id=f"pending-close-open-{account}-{symbol}",
    )
    return next(row["record_id"] for row in repo.list_position_lots() if row["record_id"] not in before)


def _deal(*, account: str = ACCOUNT, symbol: str = "NVDA", contracts: int = 1,
          execution: str = "zero-close", stock: bool = False):
    instrument = {"asset_type": "stock" if stock else "option", "market": "US", "symbol": symbol,
                  "currency": "USD"}
    if not stock:
        instrument.update(option_type="put", strike="100", multiplier="100", expiration_ymd="2026-08-21")
    return normalize_trade_deal({
        "schema_version": "trade_execution.v1", "status": "OK",
        "broker_account_ref": {"broker_id": "futu", "external_account_id": f"TEST-{account}",
            "environment": "SIMULATE", "broker_account_id": f"futu:SIMULATE:TEST-{account}",
            "account_label": account},
        "instrument_ref": instrument,
        "external_id_namespace": "futu.deal", "external_execution_id": f"{account}-{symbol}-{execution}",
        "external_order_namespace": "futu.order", "external_order_id": f"order-{account}-{symbol}-{execution}",
        "side": "buy", "position_effect": None if stock else "close", "quantity": str(contracts),
        "price": "100" if stock else "0", "currency": "USD",
        "code": f"US.{symbol}" if stock else f"US.{symbol}260821P100000",
        "occurred_at_utc": "2026-08-21T20:01:00+00:00" if stock else "2026-08-21T20:00:00+00:00",
    })


def _close(repo: SQLiteOptionPositionsRepository, **kwargs: Any) -> tuple[str, Any]:
    deal = _deal(**kwargs)
    result = resolve_lifecycle_trade_deal(deal, repo=repo, apply_changes=True, notification_status="suppressed")
    assert result is not None and result.status == "applied", result
    cases = repo.list_trade_lifecycle_cases(account=kwargs.get("account", ACCOUNT))
    case = next(row for row in cases if row["symbol"] == kwargs.get("symbol", "NVDA"))
    return case["case_id"], deal


def _rebuild(repo: SQLiteOptionPositionsRepository, *, now: int = NOW, failure_hook=None) -> dict[str, Any]:
    manifest = api.build_current_decision_projection_migration_inventory(repo.db_path, now_ms=now)
    assert manifest["readiness"] == "ready", manifest
    applied = api.apply_current_decision_projection_migration(repo.db_path, manifest, failure_hook=failure_hook)
    for row in manifest["accounts"]:
        trusted = api.read_current_decision_projection(repo, account=row["account"], now_ms=now)
        assert trusted["status"] == "trusted", trusted
        assert trusted["payload"]["lifecycle"] == _oracle(repo, account=row["account"], now=now)["lifecycle"]
    return applied


def _oracle(repo: SQLiteOptionPositionsRepository, *, account: str = ACCOUNT, now: int = NOW):
    return api.preview_current_decision_projection_oracle(
        repo, account=account, now_ms=now, assigned_stock_report=None,
    )


def _authority(repo: SQLiteOptionPositionsRepository) -> dict[str, Any]:
    cases = repo.list_trade_lifecycle_cases()
    return {"events": repo.list_trade_events(), "source_claims": repo.list_trade_lifecycle_source_consumptions(), "cases": {
        case["case_id"]: {"allocations": repo.list_trade_lifecycle_allocations(case_id=case["case_id"]),
                          "evidence": repo.list_trade_lifecycle_evidence(case_id=case["case_id"])}
        for case in cases}}


def _stored(repo: SQLiteOptionPositionsRepository, case_id: str):
    with repo._connect() as conn:  # noqa: SLF001 - persisted fact readback
        return read_lifecycle_case_decision_fact(repo, case_id=case_id, conn=conn)


def _assert_pending(repo: SQLiteOptionPositionsRepository, case_id: str, lot_id: str,
                    *, pending: int = 1, remaining: int = 0, account: str = ACCOUNT,
                    reason: str = "cause_pending"):
    direct = lifecycle_case_read_model(repo, case_id=case_id, now_ms=NOW)
    trusted = api.read_current_decision_projection(repo, account=account, now_ms=NOW)
    assert trusted["status"] == "trusted", trusted
    compact = trusted["lifecycle_by_case"][case_id]
    for model in (direct, compact):
        assert model["pending_close_contracts_by_lot"] == {lot_id: pending}
        assert model["remaining_contracts_by_lot"] == {lot_id: remaining}
        assert model["reason_state"] == reason
    oracle_case = next(row for row in _oracle(repo, account=account)["lifecycle"]["operational_cases"]
                       if row["case_id"] == case_id)
    assert oracle_case["resolution"]["pending_close_contracts_by_lot"] == {lot_id: pending}
    assert oracle_case["resolution"]["remaining_contracts_by_lot"] == {lot_id: remaining}
    assert _stored(repo, case_id)["resolution"]["pending_close_contracts_by_lot"] == {lot_id: pending}


@pytest.mark.parametrize("bootstrap", [False, True])
def test_pending_close_survives_public_rebuild_and_replay(tmp_path: Path, bootstrap: bool) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    lot_id = _open(repo)
    if bootstrap:
        _rebuild(repo)
    case_id, deal = _close(repo)
    authority = _authority(repo)
    assert repo.get_position_lot_fields(lot_id)["contracts_open"] == 0
    if bootstrap:
        _assert_pending(repo, case_id, lot_id)
    else:
        assert _stored(repo, case_id) is None
    applied = _rebuild(repo)
    if not bootstrap:
        assert applied["write_applied"] is True
    _assert_pending(repo, case_id, lot_id)
    assert api.verify_current_decision_projection_migration(repo.db_path, now_ms=NOW)["status"] == "valid"
    before = repo.read_current_decision_storage_state(ACCOUNT)
    replay = resolve_lifecycle_trade_deal(deal, repo=repo, apply_changes=True, notification_status="suppressed")
    assert replay is not None and replay.status == "skipped", replay
    assert _rebuild(repo)["write_applied"] is False
    assert repo.read_current_decision_storage_state(ACCOUNT) == before
    assert _authority(repo) == authority


def test_partial_close_allocations_are_summed_once(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    lot_id = _open(repo, contracts=3)
    _rebuild(repo)
    case_id, _ = _close(repo, execution="first")
    second_case, second_deal = _close(repo, execution="second")
    assert second_case == case_id
    direct = lifecycle_case_read_model(repo, case_id=case_id, now_ms=NOW)
    compact = api.read_current_decision_projection(repo, account=ACCOUNT, now_ms=NOW)["lifecycle_by_case"][case_id]
    for model in (direct, compact):
        assert model["pending_close_contracts_by_lot"] == {lot_id: 2}
        assert model["remaining_contracts_by_lot"] == {lot_id: 1}
        assert model["reason_state"] == "cause_pending"
    assert "direct_anchor_manifest_overlap" not in direct["lifecycle_reason_codes"]
    before = _authority(repo)
    _rebuild(repo)
    replay = resolve_lifecycle_trade_deal(second_deal, repo=repo, apply_changes=True, notification_status="suppressed")
    assert replay is not None and replay.status == "skipped", replay
    _assert_pending(repo, case_id, lot_id, pending=2, remaining=1)
    assert _authority(repo) == before


def test_split_full_close_preserves_pending_reason_source_replay_and_void(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    lot_id = _open(repo, contracts=2)
    other_lot = _open(repo, account="other", contracts=2)
    _rebuild(repo)
    other_state = repo.read_current_decision_storage_state("other")
    other_payload = api.read_current_decision_projection(repo, account="other", now_ms=NOW)["payload"]
    case_id, _ = _close(repo, execution="first")
    first_facts = api.lifecycle_case_coherent_facts(repo, case_id=case_id)
    assert first_facts["case_resolution"]["status"] == "direct"
    first_sources = {row["source_key"] for row in repo.list_trade_lifecycle_source_consumptions()}
    second_case, second_deal = _close(repo, execution="second")
    assert second_case == case_id
    facts = api.lifecycle_case_coherent_facts(repo, case_id=case_id)
    assert facts["case_resolution"]["status"] == "direct"
    assert facts["case_resolution"]["reason_codes"] == []
    assert len(facts["case_resolution"]["anchor_facts"]) == 2
    assert repo.get_position_lot_fields(lot_id)["contracts_open"] == 0
    _assert_pending(repo, case_id, lot_id, pending=2)
    before = _authority(repo)
    replay = resolve_lifecycle_trade_deal(second_deal, repo=repo, apply_changes=True, notification_status="suppressed")
    assert replay is not None and replay.status == "skipped", replay
    excess = resolve_lifecycle_trade_deal(_deal(execution="excess"), repo=repo,
                                          apply_changes=True, notification_status="suppressed")
    assert excess is not None and excess.status == "unresolved", excess
    assert excess.reason == "lifecycle_close_target_not_found"
    assert _authority(repo) == before
    assert repo.read_current_decision_storage_state("other")["generation"] == other_state["generation"]
    assert repo.get_position_lot_fields(other_lot)["contracts_open"] == 2
    _rebuild(repo)
    _assert_pending(repo, case_id, lot_id, pending=2)
    assert api.verify_current_decision_projection_migration(repo.db_path, now_ms=NOW)["status"] == "valid"
    assert _rebuild(repo)["write_applied"] is False
    assert _authority(repo) == before

    second_claim = next(row for row in before["source_claims"] if row["source_key"] not in first_sources)
    terminal = next(row["canonical_terminal_event_id"] for row in repo.list_trade_lifecycle_allocations(case_id=case_id)
                    if row["evidence_id"] == second_claim["owner_evidence_id"])
    api.record_trade_event_void(repo, event_id=terminal, reason="synthetic partial-close void")
    voided_authority = _authority(repo)
    assert voided_authority["source_claims"] == before["source_claims"]
    replay = resolve_lifecycle_trade_deal(second_deal, repo=repo, apply_changes=True, notification_status="suppressed")
    assert replay is not None and replay.status == "skipped", replay
    assert _authority(repo) == voided_authority
    _rebuild(repo)
    _assert_pending(repo, case_id, lot_id, pending=1, remaining=1)
    assert _authority(repo) == voided_authority
    assert repo.get_position_lot_fields(other_lot)["contracts_open"] == 2
    other_after = api.read_current_decision_projection(repo, account="other", now_ms=NOW)
    assert other_after["status"] == "trusted"
    # Global position source generations and publication clocks legitimately advance.
    for field in ("assigned_stock", "combo", "lifecycle", "lifecycle_quality", "source_bindings"):
        assert other_after["payload"][field] == other_payload[field]
    for field in ("position_lots_fingerprint", "position_lots_generation", "active_lot_count", "lot_count"):
        assert other_after["payload"]["position_binding"][field] == other_payload["position_binding"][field]


def test_rebuild_keeps_pending_cases_and_accounts_separate(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    expected = []
    for account, symbol, contracts in ((ACCOUNT, "NVDA", 1), (ACCOUNT, "AMD", 2), ("other", "NVDA", 3)):
        lot_id = _open(repo, account=account, symbol=symbol, contracts=contracts)
        case_id, _ = _close(repo, account=account, symbol=symbol, contracts=contracts)
        expected.append((account, case_id, lot_id, contracts))
    authority = _authority(repo)
    assert _rebuild(repo)["write_applied"] is True
    for account, case_id, lot_id, contracts in expected:
        _assert_pending(repo, case_id, lot_id, pending=contracts, account=account)
    for account in (ACCOUNT, "other"):
        expected_ids = {case_id for owner, case_id, _, _ in expected if owner == account}
        assert {row["case_id"] for row in _oracle(repo, account=account)["lifecycle"]["operational_cases"]} == expected_ids
    assert _authority(repo) == authority


def test_resolved_replacement_does_not_restore_pending_quantity(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open(repo)
    _rebuild(repo)
    case_id, _ = _close(repo)
    settlement = resolve_lifecycle_trade_deal(_deal(stock=True, contracts=100, execution="settlement"),
        repo=repo, apply_changes=True, notification_status="suppressed")
    assert settlement is not None and settlement.status == "applied", settlement
    now = NOW + 60_001
    direct = lifecycle_case_read_model(repo, case_id=case_id, now_ms=now)
    assert direct["reason_state"] == "resolved"
    assert direct["pending_close_contracts_by_lot"] == {}
    authority = _authority(repo)
    _rebuild(repo, now=now)
    trusted = api.read_current_decision_projection(repo, account=ACCOUNT, now_ms=now)
    assert trusted["status"] == "trusted"
    assert trusted["lifecycle_by_case"][case_id]["reason_state"] == "resolved"
    assert trusted["payload"]["lifecycle"] == _oracle(repo, now=now)["lifecycle"]
    assert _stored(repo, case_id)["resolution"]["pending_close_contracts_by_lot"] == {}
    assert _authority(repo) == authority


def test_voided_close_does_not_contribute_pending_quantity(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    lot_id = _open(repo)
    _rebuild(repo)
    case_id, _ = _close(repo)
    terminal = repo.list_trade_lifecycle_allocations(case_id=case_id)[0]["canonical_terminal_event_id"]
    api.record_trade_event_void(repo, event_id=terminal, reason="synthetic void regression")
    direct = lifecycle_case_read_model(repo, case_id=case_id, now_ms=NOW)
    assert direct["pending_close_contracts_by_lot"] == {}
    assert direct["remaining_contracts_by_lot"] == {lot_id: 1}
    authority = _authority(repo)
    _rebuild(repo)
    trusted = api.read_current_decision_projection(repo, account=ACCOUNT, now_ms=NOW)
    assert trusted["status"] == "trusted", trusted
    compact = trusted["lifecycle_by_case"][case_id]
    assert compact["pending_close_contracts_by_lot"] == {}
    assert compact["reason_state"] == direct["reason_state"]
    assert compact["remaining_contracts_by_lot"] == direct["remaining_contracts_by_lot"]
    assert trusted["payload"]["lifecycle"] == _oracle(repo)["lifecycle"]
    assert _stored(repo, case_id)["resolution"]["pending_close_contracts_by_lot"] == {}
    assert _authority(repo) == authority


def test_missing_close_authority_cannot_publish_empty_pending_fact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open(repo)
    _rebuild(repo)
    case_id, _ = _close(repo)
    terminal = repo.list_trade_lifecycle_allocations(case_id=case_id)[0]["canonical_terminal_event_id"]
    manifest = api.build_current_decision_projection_migration_inventory(repo.db_path, now_ms=NOW)
    storage, fact, authority = repo.read_current_decision_storage_state(ACCOUNT), _stored(repo, case_id), _authority(repo)
    original = SQLiteOptionPositionsRepository.read_lifecycle_account_rows

    def missing_event(self, **kwargs):
        rows = original(self, **kwargs)
        return {**rows, "trade_events": [row for row in rows["trade_events"] if row["event_id"] != terminal]}

    monkeypatch.setattr(SQLiteOptionPositionsRepository, "read_lifecycle_account_rows", missing_event)
    with pytest.raises(api.CurrentDecisionProjectionError, match="oracle lifecycle read model is invalid"):
        _oracle(repo)
    inventory = api.build_current_decision_projection_migration_inventory(repo.db_path, now_ms=NOW)
    assert inventory["readiness"] == "not_ready"
    assert any(reason.startswith(f"oracle_unavailable:{ACCOUNT}:") for reason in inventory["readiness_reasons"])
    with pytest.raises(ValueError, match="inventory is not ready"):
        api.apply_current_decision_projection_migration(repo.db_path, manifest)
    assert repo.read_current_decision_storage_state(ACCOUNT) == storage
    assert _stored(repo, case_id) == fact
    assert _authority(repo) == authority


def test_rebuild_failure_rolls_back_pending_fact_publication(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    lot_id = _open(repo)
    case_id, _ = _close(repo)
    storage, fact, authority = repo.read_current_decision_storage_state(ACCOUNT), _stored(repo, case_id), _authority(repo)

    def fail(stage: str) -> None:
        if stage == "after_backfill":
            raise RuntimeError("synthetic migration failure")

    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        _rebuild(repo, failure_hook=fail)
    assert repo.read_current_decision_storage_state(ACCOUNT) == storage
    assert _stored(repo, case_id) == fact
    assert _authority(repo) == authority
    assert _rebuild(repo)["write_applied"] is True
    _assert_pending(repo, case_id, lot_id)
