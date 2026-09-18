from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import sqlite3
from unittest.mock import patch

import pytest

from domain.domain.combo_identity import build_combo_identity
from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.api import recover_wheel_assignment
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_objects_atomically
from test_wheel_assignment_companions import _put_event, _assignment_payload, _open_activation


def _missing_branch(tmp_path, **payload):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    opening = _put_event(event_id="put-open", event_type="open", multiplier=10, raw_payload=payload)
    assignment = _put_event(event_id="put-assignment", event_type="assignment", multiplier=10,
                            raw_payload=_assignment_payload(10, actual_fee=False))
    persist_trade_event_objects_atomically(repo, [opening])
    persist_trade_event_objects_atomically(repo, [assignment])
    assert not repo.list_wheel_events(account="lx")
    # Install a historical eligible window in the isolated fixture only.
    _open_activation(repo)
    return repo, assignment


def _combo_missing_branch(tmp_path, *, close_call: bool = True):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    group_id = "combo_yield:lx:pair"
    metadata = {
        "strategy": "combo_yield",
        "strategy_group_id": group_id,
        "expiry_structure": "same_expiry",
    }
    opening = _put_event(
        event_id="put-open",
        event_type="open",
        multiplier=10,
        raw_payload={**metadata, "leg_role": "funding_put"},
    )
    call_key = ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="call",
        strike=110,
        expiration_ymd="2026-08-21",
        )
    call = TradeEvent(
        event_id="call-open",
        event_type="open",
        event_time_ms=1_100,
        contract_key=call_key,
        contracts=1,
        price=1,
        currency="USD",
        source="test",
        multiplier=10,
        lot_id="call-lot",
        # §9.2 step 3: the contract key no longer carries the position side; the
        # combo's participation leg is a long call, so it opens with a buy.
        raw_payload={"side": "buy", **metadata, "leg_role": "participation_call"},
    )
    persist_trade_event_objects_atomically(repo, [opening, call])
    identity = build_combo_identity(
        {
            "group_id": group_id,
            "strategy": "combo_yield",
            "account": "lx",
            "symbol": "NVDA",
            "funding_put_record_id": "put-lot",
            "funding_put_open_event_id": "put-open",
            "funding_put_contract_key": opening.to_dict()["contract_key"],
            "participation_call_record_id": "call-lot",
            "participation_call_open_event_id": "call-open",
            "participation_call_contract_key": call.to_dict()["contract_key"],
            "original_contracts": 1,
        }
    )
    repo.insert_strategy_group_identity(identity)
    _open_activation(repo)
    assignment = _put_event(
        event_id="put-assignment",
        event_type="assignment",
        multiplier=10,
        raw_payload=_assignment_payload(10),
    )
    persist_trade_event_objects_atomically(repo, [assignment])
    if close_call:
        persist_trade_event_objects_atomically(
            repo,
            [TradeEvent(
                event_id="call-expired",
                event_type="expire_close",
                event_time_ms=3_000,
                contract_key=call_key,
                contracts=1,
                price=0,
                currency="USD",
                source="test",
                multiplier=10,
                target_lot_id="call-lot",
                # §9.2 step 3: closing the combo's long participation call is a sell.
                raw_payload={"side": "sell"},
            )],
        )
    assert not repo.list_wheel_events(account="lx")
    return repo


def _recover(repo, **kwargs):
    return recover_wheel_assignment(sqlite_path=repo.db_path, account="lx", market="us",
                                    assignment_event_id="put-assignment", **kwargs)


def _files(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def test_preview_apply_and_response_loss_replay_only_append_one_wheel_event(tmp_path):
    repo, _ = _missing_branch(tmp_path)
    before = _files(tmp_path)
    preview = _recover(repo)
    assert _files(tmp_path) == before
    assert preview["status"] == "preview" and not preview["write_applied"]
    assert preview["branch"]["phase"] == "data_unavailable"
    assert {"multiplier_unproven", "assignment_cash_facts_unavailable"} <= set(preview["branch"]["reason_codes"])
    trades = repo.list_trade_events()
    lots = repo.list_position_lots()
    result = _recover(repo, apply=True, confirm=True, expected_preview_hash=preview["preview_hash"])
    assert result["status"] == "applied" and result["write_applied"]
    assert result["event"]["occurred_at_ms"] == 2_000
    assert result["event"]["recorded_at_ms"] > 2_000
    assert result["branch"]["principal_anchor"] is None
    again = _recover(repo, apply=True, confirm=True, expected_preview_hash=preview["preview_hash"])
    assert again["status"] == "already_present" and not again["write_applied"]
    assert again["event"] == result["event"]
    assert len(repo.list_wheel_events()) == 1
    assert repo.list_trade_events() == trades
    assert repo.list_position_lots() == lots


def test_completed_combo_transition_requires_explicit_flag_and_closed_sibling(tmp_path):
    complete = _combo_missing_branch(tmp_path / "complete")
    with pytest.raises(ValueError, match="ordinary CSP/CC"):
        _recover(complete)
    preview = _recover(complete, allow_completed_combo_yield=True)
    assert preview["source_transition"] == "completed_combo_yield"
    result = _recover(
        complete,
        allow_completed_combo_yield=True,
        apply=True,
        confirm=True,
        expected_preview_hash=preview["preview_hash"],
    )
    assert result["status"] == "applied"
    assert result["branch"]["direction"] == "call"

    active = _combo_missing_branch(tmp_path / "active", close_call=False)
    with pytest.raises(ValueError, match="fully closed group"):
        _recover(active, allow_completed_combo_yield=True)


def test_completed_combo_transition_still_rejects_unrelated_later_trade(tmp_path):
    repo = _combo_missing_branch(tmp_path)
    late_open = replace(
        _put_event(event_id="later", event_type="open", multiplier=10, raw_payload={}),
        event_time_ms=4_000,
        lot_id="later-lot",
    )
    persist_trade_event_objects_atomically(repo, [late_open])
    with pytest.raises(ValueError, match="subsequent facts.*later"):
        _recover(repo, allow_completed_combo_yield=True)


def test_missing_database_and_old_schema_preview_create_nothing(tmp_path):
    missing = tmp_path / "absent" / "ledger.sqlite3"
    before = _files(tmp_path)
    with pytest.raises(ValueError, match="existing ledger"):
        recover_wheel_assignment(sqlite_path=missing, account="lx", market="us", assignment_event_id="x")
    assert _files(tmp_path) == before and not missing.parent.exists()
    old = tmp_path / "old.sqlite3"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE old_table (value TEXT)")
    before = _files(tmp_path)
    for apply in (False, True):
        with pytest.raises(ValueError, match="unavailable"):
            recover_wheel_assignment(sqlite_path=old, account="lx", market="us", assignment_event_id="x",
                                     apply=apply, confirm=apply, expected_preview_hash="a" * 64)
        assert _files(tmp_path) == before


def test_hash_binds_database_identity_and_rejects_drift(tmp_path):
    repo, _ = _missing_branch(tmp_path)
    preview = _recover(repo)
    copied = tmp_path / "copy.sqlite3"
    shutil.copyfile(repo.db_path, copied)
    with pytest.raises(ValueError, match="hash changed"):
        recover_wheel_assignment(sqlite_path=copied, account="lx", market="us", assignment_event_id="put-assignment",
                                 apply=True, confirm=True, expected_preview_hash=preview["preview_hash"])
    with pytest.raises(ValueError, match="hash changed"):
        _recover(repo, apply=True, confirm=True, expected_preview_hash="a" * 64)
    assert not repo.list_wheel_events()


def test_append_and_readback_failure_roll_back(tmp_path):
    repo, _ = _missing_branch(tmp_path)
    preview = _recover(repo)
    original = SQLiteOptionPositionsRepository.append_wheel_event_once
    def fail_after_append(self, event, *, conn):
        original(self, event, conn=conn)
        raise ValueError("injected failure after append")
    with patch.object(SQLiteOptionPositionsRepository, "append_wheel_event_once", fail_after_append):
        with pytest.raises(ValueError, match="injected"):
            _recover(repo, apply=True, confirm=True, expected_preview_hash=preview["preview_hash"])
    assert not repo.list_wheel_events()


@pytest.mark.parametrize("account,market", [("sy", "us"), ("lx", "hk")])
def test_account_market_isolation(tmp_path, account, market):
    repo, _ = _missing_branch(tmp_path)
    with pytest.raises(ValueError, match="mismatch"):
        recover_wheel_assignment(sqlite_path=repo.db_path, account=account, market=market,
                                 assignment_event_id="put-assignment")
    assert not repo.list_wheel_events()


def test_void_and_later_trade_block_recovery(tmp_path):
    repo, assignment = _missing_branch(tmp_path)
    preview = _recover(repo)
    late_open = replace(_put_event(event_id="later", event_type="open", multiplier=10, raw_payload={}),
                        event_time_ms=3_000, lot_id="later-lot")
    persist_trade_event_objects_atomically(repo, [late_open])
    with pytest.raises(ValueError, match="subsequent facts.*later"):
        _recover(repo, apply=True, confirm=True, expected_preview_hash=preview["preview_hash"])
    void = replace(assignment, event_id="void-assignment", event_type="void", event_time_ms=4_000,
                   target_event_id=assignment.event_id, target_lot_id=None, raw_payload={})
    persist_trade_event_objects_atomically(repo, [void])
    with pytest.raises(ValueError, match="void"):
        _recover(repo)
    assert not repo.list_wheel_events()


def test_historical_void_recorded_after_assignment_does_not_block_recovery(tmp_path):
    repo, assignment = _missing_branch(tmp_path)
    old_open = replace(
        _put_event(event_id="old-open", event_type="open", multiplier=10, raw_payload={}),
        event_time_ms=500,
        lot_id="old-lot",
    )
    persist_trade_event_objects_atomically(repo, [old_open])
    historical_void = replace(
        assignment,
        event_id="void-old-open",
        event_type="void",
        event_time_ms=4_000,
        contracts=0,
        target_event_id=old_open.event_id,
        target_lot_id=None,
        raw_payload={},
    )
    persist_trade_event_objects_atomically(repo, [historical_void])

    assert _recover(repo)["status"] == "preview"


def test_enable_before_event_required(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(repo, [_put_event(event_id="put-open", event_type="open", multiplier=10, raw_payload={})])
    persist_trade_event_objects_atomically(repo, [_put_event(event_id="put-assignment", event_type="assignment", multiplier=10, raw_payload=_assignment_payload(10))])
    with pytest.raises(ValueError, match="historical activation"):
        _recover(repo)


def test_changed_existing_payload_is_conflict(tmp_path):
    repo, _ = _missing_branch(tmp_path)
    preview = _recover(repo)
    from domain.domain.wheel import normalize_wheel_event
    conflicting = {**preview["event"], "payload": {**preview["event"]["payload"], "principal_anchor": "99"}}
    conflicting.pop("payload_hash")
    with repo._writer_connection(begin_immediate=True) as conn:
        repo.append_wheel_event_once(normalize_wheel_event(conflicting), conn=conn)
    with pytest.raises(ValueError, match="payload conflict"):
        _recover(repo)


def test_clean_closed_wal_database_preview_preserves_source_files(tmp_path):
    repo, _ = _missing_branch(tmp_path)
    closed_path = tmp_path / "closed.sqlite3"
    from contextlib import closing
    with closing(sqlite3.connect(repo.db_path)) as source, closing(sqlite3.connect(closed_path)) as target:
        source.backup(target)
        assert target.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        assert target.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    clean_path = tmp_path / "clean-wal.sqlite3"
    shutil.copyfile(closed_path, clean_path)
    assert clean_path.read_bytes()[18:20] == b"\x02\x02"
    repo = SQLiteOptionPositionsRepository(clean_path, initialize=False)
    assert not Path(str(repo.db_path) + "-wal").exists()
    assert not Path(str(repo.db_path) + "-shm").exists()
    before = _files(tmp_path)
    assert _recover(repo)["status"] == "preview"
    assert _files(tmp_path) == before


def test_cli_preview_and_apply_guard_bind_exact_ledger(tmp_path, monkeypatch):
    import json
    from src.interfaces.cli import wheel as cli
    from tests.test_wheel_cli import _activation_environment
    _, runtime, data_config, sqlite_path = _activation_environment(tmp_path)
    repo = SQLiteOptionPositionsRepository(sqlite_path)
    persist_trade_event_objects_atomically(repo, [_put_event(event_id="put-open", event_type="open", multiplier=10, raw_payload={})])
    persist_trade_event_objects_atomically(repo, [_put_event(event_id="put-assignment", event_type="assignment", multiplier=10, raw_payload=_assignment_payload(10, actual_fee=False))])
    _open_activation(repo)
    args = ["recover", "--account", "lx", "--market", "us", "--config", str(runtime),
            "--data-config", str(data_config), "--assignment-event-id", "put-assignment", "--format", "json"]
    before = _files(tmp_path)
    preview = cli.execute(cli.parse_args(args))
    assert _files(tmp_path) == before
    guard = cli.guard_ledger_write
    bound = []
    def check_guard(**kwargs):
        result = guard(**kwargs)
        bound.append(result["active"]["sqlite_path"])
        return result
    monkeypatch.setattr(cli, "guard_ledger_write", check_guard)
    result = cli.execute(cli.parse_args(args + ["--apply", "--confirm", "--expected-preview-hash", preview["preview_hash"]]))
    assert result["write_applied"]
    assert bound == [str(sqlite_path)]
    assert json.loads(json.dumps(result))["branch"]["phase"] == "data_unavailable"


def _close_activation(repo):
    with patch("src.application.ledger.repository_assigned_stock.now_ms", return_value=5_000), repo._writer_connection(begin_immediate=True) as conn:
        repo.close_wheel_activation_window(
            market="us", account="lx", expected_current_generation=1,
            policy_hash="a" * 64, request_id="test-close", request_hash="c" * 64, conn=conn,
        )


def test_recovery_apply_then_window_close_accepts_old_hash_retry(tmp_path):
    repo, _ = _missing_branch(tmp_path)
    preview = _recover(repo)
    applied = _recover(repo, apply=True, confirm=True, expected_preview_hash=preview["preview_hash"])
    _close_activation(repo)
    repeated = _recover(repo, apply=True, confirm=True, expected_preview_hash=preview["preview_hash"])
    assert repeated["status"] == "already_present" and not repeated["write_applied"]
    assert repeated["event"] == applied["event"]
    assert repeated["branch"]["phase"] == "data_unavailable"
    assert len(repo.list_wheel_events()) == 1


def test_intake_companion_then_window_close_recovery_is_no_effect(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(repo, [_put_event(event_id="put-open", event_type="open", multiplier=10, raw_payload={})])
    _open_activation(repo)
    persist_trade_event_objects_atomically(repo, [_put_event(event_id="put-assignment", event_type="assignment", multiplier=10, raw_payload=_assignment_payload(10, actual_fee=False))])
    original = repo.list_wheel_events()
    _close_activation(repo)
    preview = _recover(repo)
    result = _recover(repo, apply=True, confirm=True, expected_preview_hash=preview["preview_hash"])
    assert result["status"] == "already_present" and not result["write_applied"]
    assert result["event"] == original[0]
    assert repo.list_wheel_events() == original


@pytest.mark.parametrize("field,value", [("policy_hash", "d" * 64), ("deactivated_at_ms", 4_000)])
def test_close_does_not_mask_conflicting_stored_window(tmp_path, field, value):
    from domain.domain.wheel import normalize_wheel_event
    repo, _ = _missing_branch(tmp_path)
    event = _recover(repo)["event"]
    event["payload"]["activation_window"][field] = value
    event.pop("payload_hash")
    with repo._writer_connection(begin_immediate=True) as conn:
        repo.append_wheel_event_once(normalize_wheel_event(event), conn=conn)
    _close_activation(repo)
    with pytest.raises(ValueError, match="activation window conflict"):
        _recover(repo)
