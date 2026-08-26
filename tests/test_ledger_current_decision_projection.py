from __future__ import annotations

import hashlib
import inspect
from copy import deepcopy
from pathlib import Path

import pytest

from domain.domain.combo_identity import build_combo_identity
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.assigned_stock import (
    assigned_stock_position_lot_row,
    project_assigned_stock_lifecycle,
)
from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger import (
    combo_reconciliation,
    current_decision_combo,
    current_decision_projection as current_projection_module,
    manual_trades,
    writer,
)
from src.application.ledger.current_decision_projection import (
    CurrentDecisionProjectionError,
    advance_assigned_stock_fact_for_trade_events,
    apply_current_decision_projection_migration,
    build_current_combo_facts,
    build_current_decision_projection,
    build_current_decision_projection_migration_inventory,
    build_current_decision_projection_payload,
    build_lifecycle_case_decision_fact,
    build_lifecycle_quality_fact,
    capture_current_decision_projection_fence,
    capture_trade_event_decision_projection_fence,
    compact_assigned_stock_view,
    current_decision_projection_migration_status,
    current_decision_projection_row,
    derive_lifecycle_quality_view,
    empty_assigned_stock_fact,
    encode_current_decision_projection,
    encode_lifecycle_case_decision_fact,
    finalize_current_decision_projection,
    preview_current_decision_projection_oracle,
    read_current_decision_projection,
    read_lifecycle_case_decision_fact,
    update_assigned_stock_fact,
    validate_assigned_stock_fact,
    validate_current_decision_projection_payload,
    validate_lifecycle_case_decision_fact,
    verify_current_decision_projection_migration,
    write_lifecycle_case_decision_fact,
)
from src.application.ledger.decision_snapshot import (
    CURRENT_DECISION_LIFECYCLE_FIELDS,
    CURRENT_DECISION_POSITION_FIELDS,
    decision_state_snapshot,
    decision_state_snapshot_fingerprint,
    decision_state_snapshot_from_rows,
)
from src.application.ledger.position_projection_runtime import (
    run_position_projection_forced_full,
    run_position_projection_in_transaction,
)
from src.application.ledger.read_only_evidence import (
    open_trade_reconciliation_evidence_repo,
)
from src.application.ledger.notification_outbox import canonical_payload_hash
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions import workflows
from src.application.trades import lifecycle_timing


def _event(
    event_id: str,
    *,
    account: str = "lx",
    symbol: str = "NVDA",
    event_time_ms: int = 1_000,
    lot_id: str | None = None,
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=event_time_ms,
        contract_key=ContractKey.from_values(
            broker="futu",
            account=account,
            underlying_symbol=symbol,
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=2,
        currency="USD",
        source="test",
        multiplier=100,
        lot_id=lot_id or f"lot-{account}",
    )


def _repo(tmp_path: Path, *, accounts: tuple[str, ...] = ("lx",)) -> SQLiteOptionPositionsRepository:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    run_position_projection_forced_full(
        repo,
        [_event(f"open-{account}", account=account) for account in accounts],
        seed_checkpoint=True,
    )
    with repo._connect() as conn:  # noqa: SLF001 - pre-migration fixture seed
        conn.executemany(
            """
            INSERT INTO current_decision_input_generations (
              account, generation, case_generation, evidence_generation,
              allocation_generation, source_consumption_generation,
              timing_generation, combo_identity_generation,
              assigned_stock_generation, updated_at_ms
            ) VALUES (?, 0, 0, 0, 0, 0, 0, 0, 0, 1)
            """,
            [(account,) for account in accounts],
        )
    return repo


def _bootstrap(
    repo: SQLiteOptionPositionsRepository,
    account: str,
    *,
    updated_at_ms: int = 10_000,
) -> dict[str, object]:
    payload = build_current_decision_projection(
        repo,
        account=account,
        updated_at_ms=updated_at_ms,
        assigned_stock_after=empty_assigned_stock_fact(account),
        all_quality_case_facts=[],
    )
    repo.upsert_current_decision_projection(current_decision_projection_row(payload))
    return payload


def _discover_projected_case(
    repo: SQLiteOptionPositionsRepository,
) -> dict[str, object]:
    discovered = writer.discover_expired_lifecycle_cases_atomically(
        repo,
        account="lx",
        observed_at_ms=1_800_000_000_000,
        apply_changes=True,
    )
    assert discovered["decision_projection"]["statuses"] == {"lx": "published"}
    return repo.get_trade_lifecycle_case(discovered["created_case_ids"][0])


def test_current_decision_migration_is_manifest_bound_atomic_and_idempotent(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _bootstrap(repo, "lx")
    lifecycle_case = _discover_projected_case(repo)
    case_id = str(lifecycle_case["case_id"])
    with repo._connect() as conn:  # noqa: SLF001 - frozen pre-migration fixture
        conn.execute("DELETE FROM current_decision_projections")
        conn.execute("DELETE FROM trade_lifecycle_case_targets")
        conn.execute(
            "UPDATE trade_lifecycle_cases SET decision_fact_json=NULL,"
            "decision_fact_sha256=NULL"
        )
        conn.execute("DELETE FROM current_decision_input_generations")

    def derived_state() -> tuple[int, ...]:
        with repo._connect() as conn:  # noqa: SLF001 - atomic rollback proof
            return tuple(
                int(conn.execute(query).fetchone()[0])
                for query in (
                    "SELECT count(*) FROM current_decision_projections",
                    "SELECT count(*) FROM current_decision_input_generations",
                    "SELECT count(*) FROM trade_lifecycle_case_targets",
                    "SELECT count(*) FROM trade_lifecycle_cases "
                    "WHERE decision_fact_json IS NOT NULL",
                )
            )

    now_ms = 1_900_000_000_000
    manifest = build_current_decision_projection_migration_inventory(
        repo.db_path,
        now_ms=now_ms,
    )
    assert manifest["read_only"] is True
    assert manifest["readiness"] == "ready", manifest["readiness_reasons"]
    assert verify_current_decision_projection_migration(
        repo.db_path,
        now_ms=now_ms,
    )["comparisons"] == [{"account": "lx", "status": "proposed"}]
    assert current_decision_projection_migration_status(
        repo.db_path,
        now_ms=now_ms,
    )["status"] == "absent"

    before_failure = derived_state()

    def fail_after_backfill(stage: str) -> None:
        if stage == "after_backfill":
            raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        apply_current_decision_projection_migration(
            repo.db_path,
            manifest,
            failure_hook=fail_after_backfill,
        )
    assert derived_state() == before_failure

    applied = apply_current_decision_projection_migration(repo.db_path, manifest)
    assert applied["write_applied"] is True
    assert read_current_decision_projection(
        repo,
        account="lx",
        now_ms=now_ms,
    )["status"] == "trusted"
    assert current_decision_projection_migration_status(
        repo.db_path,
        now_ms=now_ms + 1,
    )["status"] == "clean"
    with repo._connect() as conn:  # noqa: SLF001 - migration result proof
        assert conn.execute(
            "SELECT decision_fact_json FROM trade_lifecycle_cases WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
        assert conn.execute(
            "SELECT count(*) FROM trade_lifecycle_case_targets WHERE case_id=?",
            (case_id,),
        ).fetchone()[0] == 1
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    physical_before = current_projection_module._position_migration._file_sizes(  # noqa: SLF001
        repo.db_path
    )
    replay = apply_current_decision_projection_migration(repo.db_path, manifest)
    assert replay["write_applied"] is False
    assert current_projection_module._position_migration._file_sizes(  # noqa: SLF001
        repo.db_path
    ) == physical_before

    with repo._connect() as conn:  # noqa: SLF001 - stale manifest proof
        conn.execute(
            "UPDATE trade_lifecycle_cases SET updated_at_ms=updated_at_ms+1 "
            "WHERE case_id=?",
            (case_id,),
        )
    with pytest.raises(ValueError, match="stale"):
        apply_current_decision_projection_migration(repo.db_path, manifest)


def test_current_decision_migration_backfills_only_manifested_legacy_account(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_assigned_stock_event(
        {
            "stock_event_id": "legacy-sale",
            "event_type": "sale",
            "target_stock_lot_id": "missing-stock-lot",
            "account": "lx",
            "shares": 1,
            "price": 100,
            "currency": "USD",
            "trade_time_ms": 2_000,
        }
    )
    with repo._connect() as conn:  # noqa: SLF001 - mixed-version fixture
        for trigger in (
            "trg_current_decision_assigned_stock_account_update_guard",
            "trg_current_decision_assigned_stock_account_delete_guard",
        ):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute(
            "UPDATE assigned_stock_events SET account=NULL "
            "WHERE stock_event_id='legacy-sale'"
        )
    SQLiteOptionPositionsRepository(repo.db_path)

    manifest = build_current_decision_projection_migration_inventory(
        repo.db_path,
        now_ms=1_900_000_000_000,
    )
    assert manifest["readiness"] == "ready", manifest["readiness_reasons"]
    assert manifest["assigned_stock_legacy_account_count"] == 1
    result = apply_current_decision_projection_migration(repo.db_path, manifest)

    assert result["counts"]["assigned_accounts_backfilled"] == 1
    with repo._connect() as conn:  # noqa: SLF001 - migration result proof
        assert conn.execute(
            "SELECT account FROM assigned_stock_events "
            "WHERE stock_event_id='legacy-sale'"
        ).fetchone()[0] == "lx"


def _bind_test_timing(
    repo: SQLiteOptionPositionsRepository,
    lifecycle_case: dict[str, object],
) -> dict[str, object]:
    return lifecycle_timing.bind_lifecycle_timing_policy(
        repo,
        lifecycle_case=lifecycle_case,
        contract_metadata={
            "settlement_style": "physical",
            "underlying_security_type": "equity",
            "contract_class": "standard_equity_option",
        },
        trading_days=[
            {"date": "2026-06-22", "type": "TRADING"},
            {"date": "2026-06-23", "type": "TRADING"},
        ],
        calendar_source="test_calendar",
        calendar_observed_at_ms=1_800_000_000_000,
        apply_changes=True,
    )


def _case_fact(
    *,
    case_id: str = "case-a",
    account: str = "lx",
    legacy_generation_token: str = "1" * 64,
    reason_state: str = "not_started",
    status: str = "pending",
    legacy_evidence_gap: bool = False,
    decision_type: str | None = None,
) -> dict[str, object]:
    return build_lifecycle_case_decision_fact(
        lifecycle_case={
            "case_id": case_id,
            "account": account,
            "market": "US",
            "broker": "futu",
            "futu_account_id": "1001",
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "strike": 100,
            "expiration_ymd": "2026-06-19",
            "contract_key": f"futu|{account}|NVDA|put|short|100|2026-06-19",
            "target_contracts_by_lot": {"lot-lx": 1},
            "status": status,
            "decision_type": decision_type,
            "legacy_evidence_gap": legacy_evidence_gap,
            "derived_summary": {"reason_state": reason_state},
        },
        case_resolution={
            "case_id": case_id,
            "status": "missing",
            "reason_codes": [],
            "requested_reservations_by_lot": {},
            "effective_reservations_by_lot": {},
            "anchor_facts": [],
        },
        generation_token={
            "case_id": case_id,
            "dependency_case_ids": [case_id],
            "target_lot_ids": ["lot-lx"],
            "generation_token": legacy_generation_token,
        },
        read_model={
            "lifecycle_case_id": case_id,
            "resolved_contracts_by_lot": {"lot-lx": 0},
            "remaining_contracts_by_lot": {"lot-lx": 1},
            "resolved_contracts_by_terminal_type": {},
            "observation_start_ms": 1_000,
            "pending_until_ms": 2_000,
            "timing_policy_hash": "2" * 64,
        },
        evidence_revision=0,
        evidence_count=0,
    )


def test_lifecycle_compact_generation_token_ignores_legacy_token_and_tracks_fact() -> None:
    fact = _case_fact()
    same_fact = _case_fact(legacy_generation_token="9" * 64)
    changed_fact = _case_fact(reason_state="needs_review")

    assert fact == same_fact
    assert fact["generation"]["generation_token"] != "1" * 64
    assert (
        fact["generation"]["generation_token"]
        != changed_fact["generation"]["generation_token"]
    )

    tampered = deepcopy(fact)
    tampered["generation"]["generation_token"] = "0" * 64
    with pytest.raises(
        CurrentDecisionProjectionError,
        match="compact generation token mismatch",
    ):
        validate_lifecycle_case_decision_fact(tampered)


def _empty_assigned_report() -> dict[str, object]:
    return {
        "_all_assigned_stock_lots": [],
        "covered_call_allocations": [],
        "assigned_stock_review_rows": [],
    }


def _buy_transition(
    *,
    terminal_type: str = "assignment",
    option_type: str = "put",
    position_side: str = "short",
    terminal_event_id: str = "terminal-a",
) -> dict[str, object]:
    return {
        "kind": "buy_settlement",
        "terminal_event_id": terminal_event_id,
        "terminal_type": terminal_type,
        "option_type": option_type,
        "position_side": position_side,
        "strike": "100",
        "target_option_lot_id": "lot-source",
        "expected_contracts_open_after": 0,
        "contracts": 1,
        "multiplier": 100,
        "account": "lx",
        "broker": "futu",
        "symbol": "NVDA",
        "currency": "USD",
        "stock_settlement": {
            "side": "buy",
            "shares": 100,
            "price": "100",
            "event_time_ms": 2_000,
            "fees": "1",
        },
        "strategy_fields": {
            "strategy": None,
            "leg_role": None,
            "strategy_group_id": None,
            "yield_enhancement_mode": None,
            "source_option_leg_role": None,
        },
    }


def _final_option_lot(transition: dict[str, object]) -> dict[str, object]:
    return {
        "record_id": transition["target_option_lot_id"],
        "fields": {
            "status": (
                "open"
                if int(transition["expected_contracts_open_after"]) > 0
                else "closed"
            ),
            "contracts_open": transition["expected_contracts_open_after"],
            "source_event_id": "option-open",
            "account": transition["account"],
            "broker": transition["broker"],
            "symbol": transition["symbol"],
            "currency": transition["currency"],
            "option_type": transition["option_type"],
            "side": transition["position_side"],
            "multiplier": transition["multiplier"],
        },
    }


def _legacy_settlement_facts(
    transition: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    event_id = str(transition["terminal_event_id"])
    target_lot_id = str(transition["target_option_lot_id"])
    stock = dict(transition["stock_settlement"])
    if transition.get("stock_lot_id"):
        stock["stock_lot_id"] = transition["stock_lot_id"]
    strategy_fields = dict(transition.get("strategy_fields") or {})
    event = {
        "event_id": event_id,
        "event_type": transition["terminal_type"],
        "trade_time_ms": stock["event_time_ms"],
        "broker": transition["broker"],
        "account": transition["account"],
        "symbol": transition["symbol"],
        "option_type": transition["option_type"],
        "side": "buy" if transition["position_side"] == "short" else "sell",
        "position_effect": "close",
        "contracts": transition["contracts"],
        "price": 0,
        "strike": transition["strike"],
        "expiration_ymd": "2026-06-19",
        "currency": transition["currency"],
        "multiplier": transition["multiplier"],
        "fees": 0,
        "target_lot_id": target_lot_id,
        "raw_payload": {
            "close_type": transition["terminal_type"],
            **{
                key: value
                for key, value in strategy_fields.items()
                if key in {"strategy", "strategy_group_id"}
            },
            "stock_settlement": {
                **stock,
                "fee_provenance": {"basis": "actual", "source": "test"},
            },
        },
    }
    allocation = {
        "event_id": event_id,
        "open_event_id": f"open-{target_lot_id}",
        "source_record_id": target_lot_id,
        "close_type": transition["terminal_type"],
        "contracts_closed": transition["contracts"],
        "realized_pnl_gross": 0,
        "realized_pnl_net": 0,
        "closed_at": stock["event_time_ms"],
    }
    lot = {
        "record_id": target_lot_id,
        "open_event_id": f"open-{target_lot_id}",
        "opened_at": 1_000,
        "account": transition["account"],
        "broker": transition["broker"],
        "symbol": transition["symbol"],
        "option_type": transition["option_type"],
        "position_side": transition["position_side"],
        "currency": transition["currency"],
        "contracts": transition["contracts"],
        "remaining": transition["expected_contracts_open_after"],
        "price": 1,
        "multiplier": transition["multiplier"],
        "strike": transition["strike"],
        "expiration_ymd": "2026-06-19",
        **{
            key: value
            for key, value in strategy_fields.items()
            if key not in {"leg_role", "source_option_leg_role"}
            and value not in (None, "", {})
        },
        "leg_role": strategy_fields.get("source_option_leg_role"),
    }
    return event, allocation, lot


def _sale_after(lot: dict[str, object], *, event_id: str, shares: int) -> dict[str, object]:
    after = deepcopy(lot)
    remaining = int(lot["shares_remaining"]) - shares
    after["shares_remaining"] = remaining
    after["remaining_cost_basis"] = str(
        round(float(lot["remaining_cost_basis"]) * remaining / int(lot["shares_remaining"]), 6)
    ).rstrip("0").rstrip(".")
    event_bytes = event_id.encode()
    after["sale_fact_count"] = int(lot["sale_fact_count"]) + 1
    after["sale_fact_chain_sha256"] = hashlib.sha256(
        bytes.fromhex(str(lot["sale_fact_chain_sha256"]))
        + len(event_bytes).to_bytes(4, "big")
        + event_bytes
    ).hexdigest()
    return after


def test_projection_codec_reader_oracle_and_corruption_are_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    fact = _case_fact()
    fact_json, fact_hash = encode_lifecycle_case_decision_fact(fact)
    assert fact_json.startswith("{") and fact_hash == fact["fact_sha256"]

    current_inputs = repo.read_current_decision_projection_inputs("lx")
    assigned = empty_assigned_stock_fact("lx")
    quality = build_lifecycle_quality_fact(
        account="lx",
        all_case_facts=[fact],
        operational_case_facts=[fact],
    )
    payload = build_current_decision_projection_payload(
        account="lx",
        current_inputs=current_inputs,
        case_facts=[fact],
        assigned_stock=assigned,
        lifecycle_quality=quality,
        updated_at_ms=10_000,
    )
    payload_json, payload_hash = encode_current_decision_projection(payload)
    row = current_decision_projection_row(payload)
    assert row["payload_json"] == payload_json
    assert row["payload_sha256"] == payload_hash
    assert validate_current_decision_projection_payload(payload) == payload

    empty_payload = _bootstrap(repo, "lx")
    trusted = read_current_decision_projection(repo, account="lx", now_ms=20_000)
    assert trusted["status"] == "trusted"
    assert trusted["payload"] == empty_payload
    read_only_repo = open_trade_reconciliation_evidence_repo(repo.db_path)
    statements: list[str] = []
    original_connect = read_only_repo._connect  # noqa: SLF001 - query-only trace

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(read_only_repo, "_connect", traced_connect)
    read_only = read_current_decision_projection(
        read_only_repo,
        account="lx",
        now_ms=20_000,
    )
    assert read_only["status"] == "trusted"
    assert read_only["payload"] == empty_payload
    assert "strategy_group_identities" not in " ".join(statements).lower()
    assert read_current_decision_projection(repo, account="lx", now_ms=30_000)[
        "payload"
    ] == empty_payload
    assert preview_current_decision_projection_oracle(
        repo,
        account="lx",
        now_ms=10_000,
        assigned_stock_report=_empty_assigned_report(),
    ) == empty_payload

    with repo._connect() as conn:  # noqa: SLF001 - corruption boundary
        conn.execute(
            "UPDATE current_decision_projections SET payload_json = '{}' WHERE account = 'lx'"
        )
    unavailable = read_current_decision_projection(repo, account="lx", now_ms=30_000)
    assert unavailable["status"] == "data_unavailable"
    assert unavailable["payload"] is None


def test_reader_is_one_transaction_account_isolated_and_now_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path, accounts=("lx", "sy"))
    fact = _case_fact()
    current_inputs = repo.read_current_decision_projection_inputs("lx")
    payload = build_current_decision_projection_payload(
        account="lx",
        current_inputs=current_inputs,
        case_facts=[fact],
        assigned_stock=empty_assigned_stock_fact("lx"),
        lifecycle_quality=build_lifecycle_quality_fact(
            account="lx",
            all_case_facts=[fact],
            operational_case_facts=[fact],
        ),
        updated_at_ms=10_000,
    )
    repo.upsert_current_decision_projection(current_decision_projection_row(payload))
    _bootstrap(repo, "sy")
    with repo._connect() as conn:  # noqa: SLF001 - account-isolation corruption
        conn.execute(
            "UPDATE current_decision_projections SET payload_json = '{}' WHERE account = 'sy'"
        )

    statements: list[str] = []
    connect_count = 0
    original_connect = repo._connect  # noqa: SLF001 - read-transaction proof

    def traced_connect():
        nonlocal connect_count
        connect_count += 1
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(repo, "_connect", traced_connect)
    before = hashlib.sha256(repo.db_path.read_bytes()).hexdigest()
    pending = read_current_decision_projection(repo, account="lx", now_ms=1_500)
    elapsed = read_current_decision_projection(repo, account="lx", now_ms=2_500)
    after = hashlib.sha256(repo.db_path.read_bytes()).hexdigest()

    assert connect_count == 2
    assert sum(statement.strip().upper() == "BEGIN" for statement in statements) == 2
    assert not any(
        statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        for statement in statements
    )
    assert not any(
        name in " ".join(statements).lower()
        for name in (
            "trade_events",
            "trade_lifecycle_evidence ",
            "trade_lifecycle_allocations",
            "trade_lifecycle_source_consumptions",
            "assigned_stock_events",
            "strategy_group_identities",
        )
    )
    assert pending["lifecycle_by_case"]["case-a"]["lifecycle_state"] == "settlement_pending"
    assert elapsed["lifecycle_by_case"]["case-a"]["lifecycle_state"] == "needs_review"
    assert pending["payload"] == elapsed["payload"] == payload
    assert before == after
    assert read_current_decision_projection(repo, account="sy", now_ms=2_500)[
        "status"
    ] == "data_unavailable"


def test_legacy_snapshot_attaches_bounded_current_consumer_shadow(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _bootstrap(repo, "lx")
    rows = repo.read_decision_state_rows(account="lx")
    current = read_current_decision_projection(
        repo,
        account="lx",
        now_ms=20_000,
    )
    snapshot = decision_state_snapshot_from_rows(
        rows,
        account="lx",
        portfolio_scope_id="futu:lx",
        source_observed_at="2026-08-16T00:00:20+00:00",
        current_projection=current,
        current_decision_now_ms=20_000,
    )

    assert snapshot["snapshot_status"] == "trusted"
    assert snapshot["current_decision_read"] == current
    assert snapshot["current_decision_shadow"]["status"] == "matched"
    assert snapshot["current_decision_shadow"]["mismatch_count"] == 0
    assert {item["section"] for item in snapshot["current_decision_shadow"]["sections"]} == {
        "assigned_stock",
        "combo",
        "lifecycle",
        "position_lots",
    }
    assert "note" not in CURRENT_DECISION_POSITION_FIELDS
    assert "pairing_until_ms" not in CURRENT_DECISION_LIFECYCLE_FIELDS

    tampered = deepcopy(current)
    tampered["position_lots"][0]["fields"]["contracts_open"] = 9
    mismatch = decision_state_snapshot_from_rows(
        rows,
        account="lx",
        portfolio_scope_id="futu:lx",
        source_observed_at="2026-08-16T00:00:20+00:00",
        current_projection=tampered,
        current_decision_now_ms=20_000,
    )
    assert mismatch["snapshot_status"] == "trusted"
    assert mismatch["actionable"] is True
    assert mismatch["current_decision_shadow"]["status"] == "mismatch"
    assert len(mismatch["current_decision_shadow"]["mismatch_samples"]) <= 10
    assert decision_state_snapshot_fingerprint(mismatch) == snapshot[
        "decision_state_fingerprint"
    ]


def test_current_shadow_read_failure_does_not_change_legacy_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _bootstrap(repo, "lx")

    def _fail(*_args, **_kwargs):
        raise RuntimeError("shadow unavailable")

    monkeypatch.setattr(
        current_projection_module,
        "read_current_decision_projection",
        _fail,
    )
    snapshot = decision_state_snapshot(
        repo,
        account="lx",
        portfolio_scope_id="futu:lx",
        source_observed_at="2026-08-16T00:00:20+00:00",
        current_decision_now_ms=20_000,
    )

    assert snapshot["snapshot_status"] == "trusted"
    assert snapshot["actionable"] is True
    assert snapshot["current_decision_shadow"] == {
        "schema_version": "current_decision_shadow.v1",
        "status": "not_available",
        "reason": "current_projection_read_failed:RuntimeError",
        "mismatch_count": 0,
        "mismatch_samples": [],
        "sections": [],
    }


def test_legacy_snapshot_shadow_compares_lifecycle_quality_at_same_clock(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _bootstrap(repo, "lx")
    lifecycle_case = _discover_projected_case(repo)
    _bind_test_timing(repo, lifecycle_case)
    now_ms = 1_800_000_000_000
    rows = repo.read_decision_state_rows(account="lx")
    current = read_current_decision_projection(
        repo,
        account="lx",
        now_ms=now_ms,
    )

    snapshot = decision_state_snapshot_from_rows(
        rows,
        account="lx",
        portfolio_scope_id="futu:lx",
        source_observed_at="2027-01-15T08:00:00+00:00",
        current_projection=current,
        current_decision_now_ms=now_ms,
    )
    quality = next(
        item
        for item in snapshot["current_decision_shadow"]["sections"]
        if item["section"] == "quality:us"
    )
    assert quality["mismatch_samples"] == []
    assert quality["status"] == "matched"

    tampered = deepcopy(current)
    tampered["lifecycle_quality"]["operational_cases"][0][
        "evidence_count"
    ] += 1
    mismatch = decision_state_snapshot_from_rows(
        rows,
        account="lx",
        portfolio_scope_id="futu:lx",
        source_observed_at="2027-01-15T08:00:00+00:00",
        current_projection=tampered,
        current_decision_now_ms=now_ms,
    )
    assert mismatch["snapshot_status"] == "trusted"
    assert mismatch["current_decision_shadow"]["status"] == "mismatch"
    assert len(mismatch["current_decision_shadow"]["mismatch_samples"]) <= 10


def test_fact_and_payload_type_order_and_hash_corruption_fail_closed(tmp_path: Path) -> None:
    fact = _case_fact()
    fact_corruptions = []
    for path, value in (
        (("account",), "LX"),
        (("target_contracts_by_lot", "lot-lx"), -1),
        (("timing", "pending_until_ms"), "2000"),
        (("fact_sha256",), "0" * 64),
    ):
        candidate = deepcopy(fact)
        target = candidate
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        fact_corruptions.append(candidate)
    for candidate in fact_corruptions:
        with pytest.raises(CurrentDecisionProjectionError):
            validate_lifecycle_case_decision_fact(candidate)

    transition = _buy_transition()
    assigned = update_assigned_stock_fact(
        empty_assigned_stock_fact("lx"),
        transition=transition,
        current_position_lots=[_final_option_lot(transition)],
    )
    negative_basis = deepcopy(assigned)
    negative_basis["lots"][0]["remaining_cost_basis"] = "-1"
    with pytest.raises(CurrentDecisionProjectionError):
        validate_assigned_stock_fact(negative_basis)

    payload = _bootstrap(_repo(tmp_path), "lx")
    payload_corruptions = []
    for path, value in (
        (("normalized_account",), "LX"),
        (("source_bindings", "generation"), "0"),
        (("decision_state_fingerprint",), "0" * 64),
    ):
        candidate = deepcopy(payload)
        target = candidate
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        payload_corruptions.append(candidate)
    for candidate in payload_corruptions:
        with pytest.raises(CurrentDecisionProjectionError):
            validate_current_decision_projection_payload(candidate)


def test_indexed_builder_matches_full_oracle_for_real_lifecycle_case(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    case = {
        "case_id": "case-real",
        "case_key": "case-real",
        "account": "lx",
        "market": "US",
        "broker": "futu",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": "2026-06-19",
        "contract_key": "futu|lx|NVDA|put|short|100|2026-06-19",
        "currency": "USD",
        "multiplier": 100,
        "contracts": 1,
        "status": "waiting_settlement_evidence",
        "decision_type": "needs_review",
        "target_lot_ids": ["lot-lx"],
        "target_contracts_by_lot": {"lot-lx": 1},
        "derived_summary": {"reason_state": "not_started"},
    }
    assert repo.upsert_trade_lifecycle_case(case)
    seed = preview_current_decision_projection_oracle(
        repo,
        account="lx",
        now_ms=1_500,
        assigned_stock_report=_empty_assigned_report(),
    )
    fact = seed["lifecycle"]["operational_cases"][0]
    with repo._connect() as conn:  # noqa: SLF001 - transaction-owner proof
        assert write_lifecycle_case_decision_fact(repo, fact=fact, conn=conn)
        assert not write_lifecycle_case_decision_fact(repo, fact=fact, conn=conn)
        assert read_lifecycle_case_decision_fact(
            repo,
            case_id="case-real",
            conn=conn,
        ) == fact

    oracle = preview_current_decision_projection_oracle(
        repo,
        account="lx",
        now_ms=1_500,
        assigned_stock_report=_empty_assigned_report(),
    )
    indexed = build_current_decision_projection(
        repo,
        account="lx",
        updated_at_ms=1_500,
        assigned_stock_after=empty_assigned_stock_fact("lx"),
        all_quality_case_facts=[fact],
    )
    assert indexed == oracle


def test_discovery_and_timing_bind_publish_one_compact_case_fact(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _bootstrap(repo, "lx")
    lifecycle_case = _discover_projected_case(repo)
    assert lifecycle_case is not None
    case_id = str(lifecycle_case["case_id"])

    with repo._connect() as conn:  # noqa: SLF001 - compact fact read proof
        discovered_fact = read_lifecycle_case_decision_fact(
            repo,
            case_id=case_id,
            conn=conn,
        )
    assert discovered_fact is not None
    assert discovered_fact["resolution"]["status"] == "missing"
    trusted = read_current_decision_projection(
        repo,
        account="lx",
        now_ms=1_800_000_000_000,
    )
    assert trusted["status"] == "trusted"

    bound = _bind_test_timing(repo, lifecycle_case)
    assert bound["created"] is True
    assert bound["decision_projection"]["statuses"] == {"lx": "published"}
    with repo._connect() as conn:  # noqa: SLF001 - compact fact read proof
        timed_fact = read_lifecycle_case_decision_fact(
            repo,
            case_id=case_id,
            conn=conn,
        )
    assert timed_fact is not None
    assert timed_fact["timing"]["settlement_deadline_ms"] == bound["policy"][
        "settlement_deadline_ms"
    ]
    assert timed_fact["timing"]["timing_policy_hash"] == canonical_payload_hash(
        bound["policy"]
    )
    trusted = read_current_decision_projection(
        repo,
        account="lx",
        now_ms=1_800_000_000_000,
    )
    oracle = preview_current_decision_projection_oracle(
        repo,
        account="lx",
        now_ms=1_800_000_000_000,
        assigned_stock_report=_empty_assigned_report(),
    )
    assert trusted["payload"]["lifecycle"]["operational_cases"] == oracle[
        "lifecycle"
    ]["operational_cases"]

    before = repo.read_current_decision_storage_state("lx")
    repeated = _bind_test_timing(repo, lifecycle_case)
    assert repeated["existing"] is True
    assert repeated["decision_projection"] is None
    assert repo.read_current_decision_storage_state("lx") == before


def test_zero_price_close_publishes_direct_anchor_once(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _bootstrap(repo, "lx")
    identity = {
        "broker": "futu",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": "2026-06-19",
        "market": "US",
        "currency": "USD",
        "multiplier": 100,
    }
    evidence = {
        "evidence_id": "zero-close-1",
        "source_type": "futu_broker_deal",
        "source_event_id": "futu:lx:1001:zero-close-1",
        "evidence_type": "option_zero_price_close",
        "contracts": 1,
        "price": 0,
        "event_time_ms": 1_800_000_000_000,
        "received_at_ms": 1_800_000_000_100,
    }

    accepted = writer.accept_option_close_evidence_atomically(
        repo,
        contract_identity=identity,
        evidence=evidence,
    )
    assert accepted["decision_projection"]["statuses"] == {"lx": "published"}
    with repo._connect() as conn:  # noqa: SLF001 - compact fact read proof
        fact = read_lifecycle_case_decision_fact(
            repo,
            case_id=str(accepted["case_id"]),
            conn=conn,
        )
    assert fact is not None
    assert fact["resolution"]["status"] == "direct"
    assert fact["resolution"]["effective_reservations_by_lot"] == {"lot-lx": 1}
    assert len(fact["resolution"]["anchor_facts"]) == 1
    trusted = read_current_decision_projection(
        repo,
        account="lx",
        now_ms=1_800_000_000_000,
    )
    assert trusted["status"] == "trusted"
    oracle = preview_current_decision_projection_oracle(
        repo,
        account="lx",
        now_ms=1_800_000_000_000,
        assigned_stock_report=_empty_assigned_report(),
    )
    assert trusted["payload"]["lifecycle"] == oracle["lifecycle"]

    before = repo.read_current_decision_storage_state("lx")
    repeated = writer.accept_option_close_evidence_atomically(
        repo,
        contract_identity=identity,
        evidence=evidence,
    )
    assert repeated["status"] == "existing"
    assert "decision_projection" not in repeated
    assert repo.read_current_decision_storage_state("lx") == before


def test_timing_projection_failure_rolls_back_policy_fact_and_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    _bootstrap(repo, "lx")
    lifecycle_case = _discover_projected_case(repo)
    assert lifecycle_case is not None
    case_id = str(lifecycle_case["case_id"])
    with repo._connect() as conn:  # noqa: SLF001 - rollback fixture
        prior_fact = read_lifecycle_case_decision_fact(
            repo,
            case_id=case_id,
            conn=conn,
        )
    prior_storage = repo.read_current_decision_storage_state("lx")

    def fail_projection(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("projection fail")

    monkeypatch.setattr(repo, "upsert_current_decision_projection", fail_projection)
    with pytest.raises(RuntimeError, match="projection fail"):
        _bind_test_timing(repo, lifecycle_case)

    assert repo.get_trade_lifecycle_timing_policy(case_id) is None
    with repo._connect() as conn:  # noqa: SLF001 - rollback readback
        assert read_lifecycle_case_decision_fact(
            repo,
            case_id=case_id,
            conn=conn,
        ) == prior_fact
    assert repo.read_current_decision_storage_state("lx") == prior_storage


@pytest.mark.parametrize(
    ("terminal_type", "option_type", "position_side"),
    [("assignment", "put", "short"), ("exercise", "call", "long")],
)
def test_buy_settlement_and_exact_duplicate_are_history_free(
    monkeypatch: pytest.MonkeyPatch,
    terminal_type: str,
    option_type: str,
    position_side: str,
) -> None:
    monkeypatch.setattr(
        "src.application.positions.assigned_stock_view.build_assigned_stock_view",
        lambda *_args, **_kwargs: pytest.fail("history projector was called"),
    )
    prior = empty_assigned_stock_fact("lx")
    transition = _buy_transition(
        terminal_type=terminal_type,
        option_type=option_type,
        position_side=position_side,
    )
    updated = update_assigned_stock_fact(
        prior,
        transition=transition,
        current_position_lots=[_final_option_lot(transition)],
    )
    assert updated["lots"][0]["stock_lot_id"] == "assigned-stock-terminal-a"
    assert updated["lots"][0]["remaining_cost_basis"] == "10001"
    assert update_assigned_stock_fact(
        updated,
        transition={
            "kind": "exact_duplicate",
            "current_view_hash": updated["current_view_hash"],
        },
        current_position_lots=[],
    ) == updated


@pytest.mark.parametrize(
    ("terminal_type", "option_type", "position_side", "stock_side"),
    [
        ("assignment", "put", "short", "buy"),
        ("exercise", "call", "long", "buy"),
        ("assignment", "call", "short", "sell"),
        ("exercise", "put", "long", "sell"),
    ],
)
def test_legacy_oracle_matches_all_incremental_settlement_transitions(
    terminal_type: str,
    option_type: str,
    position_side: str,
    stock_side: str,
) -> None:
    seed = _buy_transition(terminal_event_id="seed-buy")
    seed.update(
        {
            "broker": "富途",
            "contracts": 2,
            "stock_settlement": {
                "side": "buy",
                "shares": 200,
                "price": "100",
                "event_time_ms": 2_000,
                "fees": "1",
            },
        }
    )
    transition = _buy_transition(
        terminal_type=terminal_type,
        option_type=option_type,
        position_side=position_side,
        terminal_event_id=f"{terminal_type}-{option_type}",
    )
    transition["broker"] = "富途"
    for item in (seed, transition):
        item["strategy_fields"].update(
            {
                "strategy": "combo_yield",
                "leg_role": "assigned_stock",
                "strategy_group_id": "combo-yield:lx:mixed-version",
                "yield_enhancement_mode": "vol_convexity_enhancement",
                "source_option_leg_role": "funding_put",
            }
        )
    if stock_side == "sell":
        transition = {
            key: value
            for key, value in transition.items()
            if key != "strategy_fields"
        }
        transition.update(
            {
                "kind": "sell_settlement",
                "target_option_lot_id": f"lot-{terminal_type}-{option_type}",
                "stock_lot_id": "assigned-stock-seed-buy",
                "stock_settlement": {
                    "side": "sell",
                    "shares": 100,
                    "price": "110",
                    "event_time_ms": 4_000,
                    "fees": "0",
                },
            }
        )

    transitions = [transition] if stock_side == "buy" else [seed, transition]
    current_lots = [_final_option_lot(item) for item in transitions]
    incremental = empty_assigned_stock_fact("lx")
    for item in transitions:
        incremental = update_assigned_stock_fact(
            incremental,
            transition=item,
            current_position_lots=current_lots,
        )

    legacy_facts = [_legacy_settlement_facts(item) for item in transitions]
    report = project_assigned_stock_lifecycle(
        [event for event, _allocation, _lot in legacy_facts],
        assignment_option_rows=[
            allocation for _event, allocation, _lot in legacy_facts
        ],
        option_open_lots=[lot for _event, _allocation, lot in legacy_facts],
        assigned_stock_events=[],
        quote_snapshots=[],
        account_norm="lx",
        broker_norm="富途",
        month=None,
        as_of_ms=5_000,
    )

    assert compact_assigned_stock_view(
        report,
        account="lx",
        current_position_lots=current_lots,
    ) == incremental


def test_assigned_stock_lot_adapter_preserves_retired_adjustment_mode() -> None:
    key = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-06-19",
    )
    projected = writer.project_stored_trade_events_to_position_lots(
        [
            TradeEvent(
                event_id="mixed-open",
                event_type="open",
                event_time_ms=1_000,
                contract_key=key,
                contracts=1,
                price=1,
                currency="USD",
                source="legacy",
                multiplier=100,
                lot_id="mixed-lot",
                raw_payload={
                    "strategy": "combo_yield",
                    "strategy_group_id": "combo-yield:lx:mixed-version",
                },
            ),
            TradeEvent(
                event_id="mixed-adjust",
                event_type="adjust",
                event_time_ms=2_000,
                contract_key=key,
                contracts=0,
                price=0,
                currency="USD",
                source="legacy",
                multiplier=100,
                target_lot_id="mixed-lot",
                raw_payload={
                    "patch": {"yield_enhancement_mode": "vol_convexity_enhancement"}
                },
            ),
        ]
    )
    current = projected.lots[0]
    row = assigned_stock_position_lot_row(
        projected.ledger_projection.lots[0],
        current_fields=current.fields,
        at_ms=3_000,
    )

    assert row["yield_enhancement_mode"] == "vol_convexity_enhancement"


def test_assigned_oracle_does_not_restore_mode_absent_from_bound_source_lot() -> None:
    transition = _buy_transition()
    transition["broker"] = "富途"
    transition["strategy_fields"].update(
        {
            "strategy": "combo_yield",
            "leg_role": "assigned_stock",
            "strategy_group_id": "combo-yield:lx:migrated",
            "source_option_leg_role": "funding_put",
        }
    )
    terminal, allocation, source_lot = _legacy_settlement_facts(transition)
    source_open = {
        "event_id": source_lot["open_event_id"],
        "event_type": "open",
        "trade_time_ms": 1_000,
        "broker": "富途",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "side": "sell",
        "position_effect": "open",
        "contracts": 1,
        "price": 1,
        "strike": 100,
        "expiration_ymd": "2026-06-19",
        "currency": "USD",
        "multiplier": 100,
        "raw_payload": {
            "strategy": "combo_yield",
            "strategy_group_id": "combo-yield:lx:migrated",
            "yield_enhancement_mode": "vol_convexity_enhancement",
        },
    }
    report = project_assigned_stock_lifecycle(
        [source_open, terminal],
        assignment_option_rows=[allocation],
        option_open_lots=[source_lot],
        assigned_stock_events=[],
        quote_snapshots=[],
        account_norm="lx",
        broker_norm="富途",
        month=None,
        as_of_ms=5_000,
    )
    incremental = update_assigned_stock_fact(
        empty_assigned_stock_fact("lx"),
        transition=transition,
        current_position_lots=[_final_option_lot(transition)],
    )

    assert "yield_enhancement_mode" not in report["assigned_stock_lots"][0]
    assert compact_assigned_stock_view(
        report,
        account="lx",
        current_position_lots=[_final_option_lot(transition)],
    ) == incremental


def test_settlement_quantity_mismatch_fails_closed_on_both_authorities() -> None:
    transition = _buy_transition(
        terminal_type="exercise",
        option_type="call",
        position_side="long",
        terminal_event_id="bad-exercise",
    )
    transition["broker"] = "富途"
    transition["stock_settlement"] = {
        **dict(transition["stock_settlement"]),
        "shares": 50,
    }
    event, allocation, lot = _legacy_settlement_facts(transition)

    report = project_assigned_stock_lifecycle(
        [event],
        assignment_option_rows=[allocation],
        option_open_lots=[lot],
        assigned_stock_events=[],
        quote_snapshots=[],
        account_norm="lx",
        broker_norm="富途",
        month=None,
        as_of_ms=5_000,
    )

    assert report["assigned_stock_lots"] == []
    assert report["assigned_stock_review_rows"][0]["status"] == "incomplete_inventory_basis"
    allocation["source_record_id"] = "missing-option-lot"
    binding_event = deepcopy(event)
    binding_event["raw_payload"]["stock_settlement"]["shares"] = 100
    binding_report = project_assigned_stock_lifecycle(
        [binding_event],
        assignment_option_rows=[allocation],
        option_open_lots=[lot],
        assigned_stock_events=[],
        quote_snapshots=[],
        account_norm="lx",
        broker_norm="富途",
        month=None,
        as_of_ms=5_000,
    )
    assert binding_report["assigned_stock_lots"] == []
    assert binding_report["assigned_stock_review_rows"][0]["status"] == "incomplete_inventory_basis"
    with pytest.raises(CurrentDecisionProjectionError, match="quantity mismatch"):
        update_assigned_stock_fact(
            empty_assigned_stock_fact("lx"),
            transition=transition,
            current_position_lots=[_final_option_lot(transition)],
        )


@pytest.mark.parametrize(
    "malformed",
    ("fractional_time", "terminal_type", "open_event", "duplicate_allocation"),
)
def test_legacy_settlement_rejects_malformed_time_or_allocation_binding(
    malformed: str,
) -> None:
    transition = _buy_transition(
        terminal_type="exercise",
        option_type="call",
        position_side="long",
        terminal_event_id="malformed-exercise",
    )
    transition["broker"] = "富途"
    event, allocation, lot = _legacy_settlement_facts(transition)
    allocations = [allocation]
    if malformed == "fractional_time":
        event["raw_payload"]["stock_settlement"]["event_time_ms"] = "4000.5"
    elif malformed == "terminal_type":
        allocation["close_type"] = "assignment"
    elif malformed == "open_event":
        allocation["open_event_id"] = "different-open-event"
    else:
        event["contracts"] = 2
        event["raw_payload"]["stock_settlement"]["shares"] = 200
        lot["contracts"] = 2
        allocation["contracts_closed"] = 1
        allocations.append(deepcopy(allocation))

    report = project_assigned_stock_lifecycle(
        [event],
        assignment_option_rows=allocations,
        option_open_lots=[lot],
        assigned_stock_events=[],
        quote_snapshots=[],
        account_norm="lx",
        broker_norm="富途",
        month=None,
        as_of_ms=5_000,
    )

    assert report["assigned_stock_lots"] == []
    assert report["assigned_stock_review_rows"]


def test_hkd_settlement_fee_and_embedded_time_match_legacy_oracle() -> None:
    transition = _buy_transition(terminal_event_id="hk-assignment")
    transition.update(
        {
            "broker": "富途",
            "symbol": "0700.HK",
            "currency": "HKD",
            "stock_settlement": {
                "side": "buy",
                "shares": 100,
                "price": "100",
                "event_time_ms": 4_000,
                "fees": 0,
            },
        }
    )
    current_lot = _final_option_lot(transition)
    current_lot["fields"]["opened_at"] = 1_000
    event = TradeEvent(
        event_id="hk-assignment",
        event_type="assignment",
        event_time_ms=2_000,
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="0700.HK",
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=0,
        currency="HKD",
        source="test",
        multiplier=100,
        target_lot_id="lot-source",
        raw_payload={
            "close_type": "assignment",
            "stock_settlement": dict(transition["stock_settlement"]),
        },
    )
    incremental = advance_assigned_stock_fact_for_trade_events(
        empty_assigned_stock_fact("lx"),
        event_mutations=((event, True),),
        current_position_lots=[current_lot],
    )
    legacy_event, allocation, legacy_lot = _legacy_settlement_facts(transition)
    legacy_event["trade_time_ms"] = 2_000
    legacy_event["raw_payload"]["stock_settlement"].pop("fee_provenance")
    report = project_assigned_stock_lifecycle(
        [legacy_event],
        assignment_option_rows=[allocation],
        option_open_lots=[legacy_lot],
        assigned_stock_events=[],
        quote_snapshots=[],
        account_norm="lx",
        broker_norm="富途",
        month=None,
        as_of_ms=5_000,
    )

    assert incremental["lots"][0]["assigned_at_ms"] == 4_000
    assert incremental["lots"][0]["remaining_cost_basis"] == "10000"
    assert compact_assigned_stock_view(
        report,
        account="lx",
        current_position_lots=[current_lot],
    ) == incremental


def test_sale_sell_and_covered_call_transitions_are_bounded() -> None:
    transition = _buy_transition()
    assigned = update_assigned_stock_fact(
        empty_assigned_stock_fact("lx"),
        transition=transition,
        current_position_lots=[_final_option_lot(transition)],
    )
    lot = dict(assigned["lots"][0])
    after = _sale_after(lot, event_id="sale-a", shares=40)
    sold = update_assigned_stock_fact(
        assigned,
        transition={
            "kind": "assigned_stock_sale",
            "stock_event_id": "sale-a",
            "stock_lot_id": lot["stock_lot_id"],
            "shares": 40,
            "trade_time_ms": 3_000,
            "lot_after": after,
        },
        current_position_lots=[],
    )
    assert sold["lots"][0]["shares_remaining"] == 60
    assert sold["applied_sale_fact_count"] == 1

    linked = update_assigned_stock_fact(
        sold,
        transition={
            "kind": "covered_call_linkage",
            "allocations": [
                {
                    "open_event_id": "call-open",
                    "stock_lot_id": lot["stock_lot_id"],
                    "account": "lx",
                    "broker": "futu",
                    "symbol": "NVDA",
                    "currency": "USD",
                    "shares": 60,
                    "start_at_ms": 3_100,
                    "end_at_ms": None,
                    "allocation_status": "explicit",
                    "linkage_basis": "stock_lot_id",
                }
            ],
        },
        current_position_lots=[
            {
                "record_id": "lot-call",
                "fields": {
                    "status": "open",
                    "contracts_open": 1,
                    "source_event_id": "call-open",
                    "account": "lx",
                    "broker": "futu",
                    "symbol": "NVDA",
                    "currency": "USD",
                    "option_type": "call",
                    "side": "short",
                    "multiplier": 100,
                },
            }
        ],
    )
    assert linked["covered_call_allocations"][0]["open_event_id"] == "call-open"

    for terminal_type, option_type, position_side in (
        ("assignment", "call", "short"),
        ("exercise", "put", "long"),
    ):
        sell = {
            key: value
            for key, value in _buy_transition().items()
            if key != "strategy_fields"
        }
        sell.update(
            {
                "kind": "sell_settlement",
                "terminal_event_id": f"terminal-{terminal_type}",
                "terminal_type": terminal_type,
                "option_type": option_type,
                "position_side": position_side,
                "target_option_lot_id": "lot-call",
                "multiplier": 60,
                "stock_lot_id": lot["stock_lot_id"],
                "stock_settlement": {
                    "side": "sell",
                    "shares": 60,
                    "price": "100",
                    "event_time_ms": 4_000,
                    "fees": "0",
                },
            }
        )
        closed = update_assigned_stock_fact(
            linked,
            transition=sell,
            current_position_lots=[_final_option_lot(sell)],
        )
        assert closed["lots"] == []
        assert closed["covered_call_allocations"] == []


def test_assigned_transition_validates_final_lot_numeric_and_linkage_identity() -> None:
    transition = _buy_transition()
    transition["expected_contracts_open_after"] = 1
    final_lot = {
        "record_id": "lot-source",
        "fields": {
            "status": "open",
            "contracts_open": 1,
            "source_event_id": "option-open",
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "currency": "USD",
            "option_type": "put",
            "side": "short",
            "multiplier": 100,
        },
    }
    update_assigned_stock_fact(
        empty_assigned_stock_fact("lx"),
        transition=transition,
        current_position_lots=[final_lot],
    )
    with pytest.raises(CurrentDecisionProjectionError, match="lot is missing"):
        update_assigned_stock_fact(
            empty_assigned_stock_fact("lx"),
            transition=_buy_transition(),
            current_position_lots=[],
        )

    bad_price = deepcopy(transition)
    bad_price["stock_settlement"]["price"] = "-1"
    bad_identity = deepcopy(final_lot)
    bad_identity["fields"]["symbol"] = "MSFT"
    for candidate, lots in (
        (bad_price, [final_lot]),
        (transition, [bad_identity]),
    ):
        with pytest.raises(CurrentDecisionProjectionError):
            update_assigned_stock_fact(
                empty_assigned_stock_fact("lx"),
                transition=candidate,
                current_position_lots=lots,
            )


def test_covered_call_linkage_enforces_aggregate_option_capacity() -> None:
    assigned = empty_assigned_stock_fact("lx")
    for suffix in ("a", "b"):
        transition = _buy_transition(terminal_event_id=f"terminal-{suffix}")
        transition["target_option_lot_id"] = f"lot-source-{suffix}"
        assigned = update_assigned_stock_fact(
            assigned,
            transition=transition,
            current_position_lots=[_final_option_lot(transition)],
        )
    call_lot = {
        "record_id": "lot-call",
        "fields": {
            "status": "open",
            "contracts_open": 1,
            "source_event_id": "call-open",
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "currency": "USD",
            "option_type": "call",
            "side": "short",
            "multiplier": 100,
        },
    }

    def allocations(shares: int) -> list[dict[str, object]]:
        return [
            {
                "open_event_id": "call-open",
                "stock_lot_id": lot["stock_lot_id"],
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "currency": "USD",
                "shares": shares,
                "start_at_ms": 3_100 + index,
                "end_at_ms": None,
                "allocation_status": "explicit",
                "linkage_basis": "stock_lot_id",
            }
            for index, lot in enumerate(assigned["lots"])
        ]

    exact = update_assigned_stock_fact(
        assigned,
        transition={"kind": "covered_call_linkage", "allocations": allocations(50)},
        current_position_lots=[call_lot],
    )
    assert sum(row["shares"] for row in exact["covered_call_allocations"]) == 100
    with pytest.raises(CurrentDecisionProjectionError, match="option quantity"):
        update_assigned_stock_fact(
            assigned,
            transition={
                "kind": "covered_call_linkage",
                "allocations": allocations(60),
            },
            current_position_lots=[call_lot],
        )


def test_covered_call_linkage_without_current_identity_fails_closed() -> None:
    transition = _buy_transition()
    assigned = update_assigned_stock_fact(
        empty_assigned_stock_fact("lx"),
        transition=transition,
        current_position_lots=[_final_option_lot(transition)],
    )
    call_event = TradeEvent(
        event_id="call-open-no-stock-identity",
        event_type="open",
        event_time_ms=3_000,
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type="call",
            position_side="short",
            strike=110,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=2,
        currency="USD",
        source="test",
        multiplier=100,
        lot_id="lot-call-no-stock-identity",
    )
    call_lot = {
        "record_id": "lot-call-no-stock-identity",
        "fields": {
            "status": "open",
            "contracts_open": 1,
            "source_event_id": call_event.event_id,
            "opened_at": 3_000,
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "currency": "USD",
            "option_type": "call",
            "side": "short",
            "multiplier": 100,
        },
    }

    with pytest.raises(CurrentDecisionProjectionError, match="identity is missing"):
        advance_assigned_stock_fact_for_trade_events(
            assigned,
            event_mutations=((call_event, True),),
            current_position_lots=[call_lot],
        )


def test_resolved_covered_call_identity_removes_stale_review() -> None:
    transition = _buy_transition()
    transition["strategy_fields"] = {
        **dict(transition["strategy_fields"]),
        "leg_role": "assigned_stock",
        "strategy_group_id": "group-a",
    }
    assigned = update_assigned_stock_fact(
        empty_assigned_stock_fact("lx"),
        transition=transition,
        current_position_lots=[_final_option_lot(transition)],
    )
    assigned["review_facts"] = [
        {
            "status": "covered_call_unallocated",
            "event_id": "call-open",
            "stock_lot_id": None,
            "stock_event_id": None,
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "details_sha256": canonical_sha256({"required_shares": 100}),
        }
    ]
    assigned["current_view_hash"] = canonical_sha256(
        {key: value for key, value in assigned.items() if key != "current_view_hash"}
    )
    call = TradeEvent(
        event_id="call-open",
        event_type="open",
        event_time_ms=3_000,
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type="call",
            position_side="short",
            strike=110,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=2,
        currency="USD",
        source="test",
        multiplier=100,
        lot_id="lot-call",
    )
    call_lot = {
        "record_id": "lot-call",
        "fields": {
            "status": "open",
            "contracts_open": 1,
            "source_event_id": "call-open",
            "opened_at": 3_000,
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "currency": "USD",
            "option_type": "call",
            "side": "short",
            "multiplier": 100,
            "strategy_group_id": "group-a",
        },
    }

    repaired = advance_assigned_stock_fact_for_trade_events(
        assigned,
        event_mutations=((call, True),),
        current_position_lots=[call_lot],
    )

    assert len(repaired["covered_call_allocations"]) == 1
    assert repaired["review_facts"] == []


def test_covered_call_group_change_revalidates_prior_allocation() -> None:
    transition = _buy_transition(terminal_event_id="assign-a")
    transition["target_option_lot_id"] = "put-lot-a"
    transition["strategy_fields"] = {
        **dict(transition["strategy_fields"]),
        "leg_role": "assigned_stock",
        "strategy_group_id": "group-a",
    }
    alternate = _buy_transition(terminal_event_id="assign-b")
    alternate["target_option_lot_id"] = "put-lot-b"
    alternate["strategy_fields"] = {
        **dict(alternate["strategy_fields"]),
        "leg_role": "assigned_stock",
        "strategy_group_id": "group-b",
    }
    final_lots = [_final_option_lot(item) for item in (transition, alternate)]
    assigned = update_assigned_stock_fact(
        empty_assigned_stock_fact("lx"),
        transition=transition,
        current_position_lots=final_lots,
    )
    assigned = update_assigned_stock_fact(
        assigned,
        transition=alternate,
        current_position_lots=final_lots,
    )
    call = TradeEvent(
        event_id="call-open",
        event_type="open",
        event_time_ms=3_000,
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type="call",
            position_side="short",
            strike=110,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=2,
        currency="USD",
        source="test",
        multiplier=100,
        lot_id="lot-call",
    )
    call_lot = {
        "record_id": "lot-call",
        "fields": {
            "status": "open",
            "contracts_open": 1,
            "source_event_id": "call-open",
            "opened_at": 3_000,
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "currency": "USD",
            "option_type": "call",
            "side": "short",
            "multiplier": 100,
            "strategy_group_id": "group-a",
        },
    }
    linked = advance_assigned_stock_fact_for_trade_events(
        assigned,
        event_mutations=((call, True),),
        current_position_lots=[call_lot],
    )
    assert linked["covered_call_allocations"][0]["linkage_basis"] == "strategy_group"
    linked["review_facts"] = [
        {
            "status": "covered_call_unallocated",
            "event_id": "call-open",
            "stock_lot_id": None,
            "stock_event_id": None,
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "details_sha256": canonical_sha256({"required_shares": 100}),
        }
    ]
    linked["current_view_hash"] = canonical_sha256(
        {key: value for key, value in linked.items() if key != "current_view_hash"}
    )
    call_lot["fields"]["strategy_group_id"] = "group-b"

    with pytest.raises(CurrentDecisionProjectionError, match="identity mismatch"):
        advance_assigned_stock_fact_for_trade_events(
            linked,
            event_mutations=(),
            current_position_lots=[call_lot],
        )
    assert len(linked["covered_call_allocations"]) == 1
    assert [row["status"] for row in linked["review_facts"]] == [
        "covered_call_unallocated"
    ]


def test_covered_call_explicit_stock_lot_survives_restart_with_conflicting_group() -> None:
    transitions = []
    for suffix in ("a", "b"):
        transition = _buy_transition(terminal_event_id=f"assign-{suffix}")
        transition["target_option_lot_id"] = f"put-lot-{suffix}"
        transition["strategy_fields"] = {
            **dict(transition["strategy_fields"]),
            "leg_role": "assigned_stock",
            "strategy_group_id": f"group-{suffix}",
        }
        transitions.append(transition)
    final_lots = [_final_option_lot(transition) for transition in transitions]
    assigned = empty_assigned_stock_fact("lx")
    for transition in transitions:
        assigned = update_assigned_stock_fact(
            assigned,
            transition=transition,
            current_position_lots=final_lots,
        )

    call = TradeEvent(
        event_id="call-open",
        event_type="open",
        event_time_ms=3_000,
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type="call",
            position_side="short",
            strike=110,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=2,
        currency="USD",
        source="test",
        multiplier=100,
        lot_id="call-lot",
        raw_payload={"stock_lot_id": "assigned-stock-assign-a"},
    )
    call_lot = {
        "record_id": "call-lot",
        "fields": {
            "status": "open",
            "contracts_open": 1,
            "source_event_id": "call-open",
            "opened_at": 3_000,
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "currency": "USD",
            "option_type": "call",
            "side": "short",
            "multiplier": 100,
            "strategy_group_id": "group-b",
        },
    }

    first = advance_assigned_stock_fact_for_trade_events(
        assigned,
        event_mutations=((call, True),),
        current_position_lots=[call_lot],
    )
    allocation = first["covered_call_allocations"][0]
    assert allocation["stock_lot_id"] == "assigned-stock-assign-a"
    assert allocation["linkage_basis"] == "stock_lot_id"

    assert advance_assigned_stock_fact_for_trade_events(
        first,
        event_mutations=(),
        current_position_lots=[call_lot],
    ) == first

    unprovenanced = deepcopy(first)
    unprovenanced["covered_call_allocations"][0].pop("linkage_basis")
    unprovenanced["current_view_hash"] = canonical_sha256(
        {key: value for key, value in unprovenanced.items() if key != "current_view_hash"}
    )
    with pytest.raises(CurrentDecisionProjectionError, match="allocation shape"):
        advance_assigned_stock_fact_for_trade_events(
            unprovenanced,
            event_mutations=(),
            current_position_lots=[call_lot],
        )


def test_covered_call_candidate_resolution_is_linear_in_current_lots() -> None:
    class CountingAccount(str):
        comparisons = 0
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            type(self).comparisons += 1
            return super().__eq__(other)

    template_transition = _buy_transition()
    template_transition["strategy_fields"] = {
        **dict(template_transition["strategy_fields"]),
        "leg_role": "assigned_stock",
        "strategy_group_id": "group-000",
    }
    template = update_assigned_stock_fact(
        empty_assigned_stock_fact("lx"),
        transition=template_transition,
        current_position_lots=[_final_option_lot(template_transition)],
    )["lots"][0]
    size = 40
    lots = [
        {
            **template,
            "stock_lot_id": f"assigned-stock-{index:03d}",
            "source_assignment_event_id": f"assignment-{index:03d}",
            "source_option_lot_id": f"put-lot-{index:03d}",
            "account": CountingAccount("lx"),
            "strategy_group_id": f"group-{index:03d}",
        }
        for index in range(size)
    ]
    assigned = empty_assigned_stock_fact("lx")
    assigned["lots"] = lots
    assigned["applied_sale_fact_chain_sha256"] = canonical_sha256(
        [
            {
                "stock_lot_id": row["stock_lot_id"],
                "sale_fact_count": 0,
                "sale_fact_chain_sha256": row["sale_fact_chain_sha256"],
            }
            for row in lots
        ]
    )
    assigned["current_view_hash"] = canonical_sha256(
        {key: value for key, value in assigned.items() if key != "current_view_hash"}
    )
    call_lots = [
        {
            "record_id": f"call-lot-{index:03d}",
            "fields": {
                "status": "open",
                "contracts_open": 1,
                "source_event_id": f"call-open-{index:03d}",
                "opened_at": 3_000,
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "currency": "USD",
                "option_type": "call",
                "side": "short",
                "multiplier": 100,
                "strategy_group_id": f"group-{index:03d}",
            },
        }
        for index in range(size)
    ]

    updated = advance_assigned_stock_fact_for_trade_events(
        assigned,
        event_mutations=(),
        current_position_lots=call_lots,
    )

    assert len(updated["covered_call_allocations"]) == size
    assert CountingAccount.comparisons < size * 10


@pytest.mark.parametrize(
    ("original", "put_open", "call_open", "assigned", "expected"),
    [
        (1, 1, 1, False, "active_combo"),
        (2, 1, 2, False, "partially_decomposed"),
        (1, 0, 1, True, "assigned_stock_with_residual_call"),
        (1, 0, 0, True, "assigned_stock_only"),
    ],
)
def test_combo_facts_use_retained_terminal_lots(
    original: int,
    put_open: int,
    call_open: int,
    assigned: bool,
    expected: str,
) -> None:
    group_id = "combo-yield:lx:group-a"
    identity = build_combo_identity(
        {
            "group_id": group_id,
            "strategy": "combo_yield",
            "account": "lx",
            "symbol": "NVDA",
            "funding_put_record_id": "put-lot",
            "funding_put_open_event_id": "put-open",
            "funding_put_contract_key": "put-key",
            "participation_call_record_id": "call-lot",
            "participation_call_open_event_id": "call-open",
            "participation_call_contract_key": "call-key",
            "original_contracts": original,
        }
    )
    assigned_fact = empty_assigned_stock_fact("lx")
    if assigned:
        transition = _buy_transition()
        transition["strategy_fields"] = {
            "strategy": "combo_yield",
            "leg_role": "funding_put",
            "strategy_group_id": group_id,
            "yield_enhancement_mode": "combo_yield",
            "source_option_leg_role": "funding_put",
        }
        assigned_fact = update_assigned_stock_fact(
            assigned_fact,
            transition=transition,
            current_position_lots=[_final_option_lot(transition)],
        )
    lots = [
        {
            "record_id": record_id,
            "fields": {
                "status": "open" if contracts_open else "closed",
                "contracts_open": contracts_open,
                "source_event_id": open_event_id,
                "account": "lx",
                "symbol": "NVDA",
                "strategy_group_id": group_id,
                "leg_role": role,
            },
        }
        for record_id, open_event_id, role, contracts_open in (
            ("put-lot", "put-open", "funding_put", put_open),
            ("call-lot", "call-open", "participation_call", call_open),
        )
    ]
    groups = build_current_combo_facts(
        account="lx",
        current_position_lots=lots,
        identities=[identity],
        assigned_stock=assigned_fact,
    )["current_groups"]
    assert groups[0]["status"] == expected
    assert [row["record_id"] for row in groups[0]["active_member_bindings"]] == sorted(
        record_id
        for record_id, contracts_open in (("put-lot", put_open), ("call-lot", call_open))
        if contracts_open
    )


def test_terminal_quality_counts_remain_in_derived_summary() -> None:
    operational = _case_fact()
    facts = [
        _case_fact(
            case_id="terminal-trusted",
            status="ledger_written",
            reason_state="resolved",
        ),
        _case_fact(
            case_id="terminal-legacy",
            status="ledger_written",
            reason_state="resolved",
            legacy_evidence_gap=True,
        ),
        _case_fact(
            case_id="terminal-external",
            status="ledger_written",
            reason_state="resolved",
            decision_type="external_adjustment",
        ),
    ]
    quality = build_lifecycle_quality_fact(
        account="lx",
        all_case_facts=[operational, *facts],
        operational_case_facts=[operational],
    )
    view = derive_lifecycle_quality_view(quality, now_ms=1_500)
    assert view["operational_status_counts"] == {
        "partial": 1,
        "trusted": 1,
        "unavailable": 1,
        "untrusted": 1,
    }
    assert view["blocked_consumer_counts"] == {
        "close_advice": 1,
        "lifecycle_report": 1,
        "option_performance": 2,
    }
    with pytest.raises(CurrentDecisionProjectionError, match="missing detail"):
        derive_lifecycle_quality_view(
            build_lifecycle_quality_fact(
                account="lx",
                all_case_facts=[operational],
                operational_case_facts=[],
            ),
            now_ms=1_500,
        )
    foreign = _case_fact(account="sy")
    with pytest.raises(CurrentDecisionProjectionError, match="account mismatch"):
        build_lifecycle_quality_fact(
            account="lx",
            all_case_facts=[foreign],
            operational_case_facts=[foreign],
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"kind": "unsupported"},
        {
            "kind": "exact_duplicate",
            "current_view_hash": "0" * 64,
        },
    ],
)
def test_unsupported_or_conflicting_assigned_transition_fails_closed(
    mutation: dict[str, object],
) -> None:
    with pytest.raises(CurrentDecisionProjectionError):
        update_assigned_stock_fact(
            empty_assigned_stock_fact("lx"),
            transition=mutation,
            current_position_lots=[],
        )


def test_fence_finalizer_skips_unchanged_and_rebuilds_global_fanout_once(
    tmp_path: Path,
) -> None:
    accounts = tuple(f"a{index}" for index in range(10))
    repo = _repo(tmp_path, accounts=accounts)
    for account in accounts:
        _bootstrap(repo, account)
    fence = capture_current_decision_projection_fence(repo, accounts=accounts)

    with repo._connect() as conn:  # noqa: SLF001 - transaction owner contract
        conn.execute("BEGIN IMMEDIATE")
        unchanged = finalize_current_decision_projection(
            repo,
            fence=fence,
            updated_at_ms=11_000,
            conn=conn,
        )
        assert unchanged["projection_dml_count"] == 0
        assert set(unchanged["statuses"].values()) == {"not_required"}
        conn.rollback()

    fence = capture_current_decision_projection_fence(repo, accounts=accounts)
    with repo._connect() as conn:  # noqa: SLF001 - transaction owner contract
        conn.execute("BEGIN IMMEDIATE")
        run_position_projection_in_transaction(
            repo,
            [
                _event(
                    "open-global",
                    account="a0",
                    symbol="MSFT",
                    event_time_ms=2_000,
                    lot_id="lot-global",
                )
            ],
            conn=conn,
            mode="forced_full",
        )
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        published = finalize_current_decision_projection(
            repo,
            fence=fence,
            updated_at_ms=12_000,
            conn=conn,
        )
        conn.set_trace_callback(None)
        conn.commit()
    assert published["published_accounts"] == list(accounts)
    assert published["projection_dml_count"] == len(accounts)
    projection_dml = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("INSERT INTO CURRENT_DECISION_PROJECTIONS")
    ]
    assert len(projection_dml) == len(accounts)
    assert all(
        read_current_decision_projection(repo, account=account, now_ms=13_000)[
            "status"
        ]
        == "trusted"
        for account in accounts
    )


@pytest.mark.parametrize("history_count", [100, 1_000])
def test_fence_finalizer_does_not_scan_unreferenced_combo_identity_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history_count: int,
) -> None:
    repo = _repo(tmp_path)
    run_position_projection_forced_full(
        repo,
        [_event("identity-anchor", event_time_ms=2_000, lot_id="identity-anchor")],
    )
    with repo._connect() as conn:  # noqa: SLF001 - historical fixture seed
        for index in range(history_count):
            repo.insert_strategy_group_identity(
                build_combo_identity(
                    {
                        "group_id": f"history-{index:04d}",
                        "strategy": "combo_yield",
                        "account": "lx",
                        "symbol": "NVDA",
                        "funding_put_record_id": f"put-{index:04d}",
                        "funding_put_open_event_id": "open-lx",
                        "funding_put_contract_key": {"id": f"put-{index:04d}"},
                        "participation_call_record_id": f"call-{index:04d}",
                        "participation_call_open_event_id": "identity-anchor",
                        "participation_call_contract_key": {
                            "id": f"call-{index:04d}"
                        },
                        "original_contracts": 1,
                    }
                ),
                conn=conn,
            )
    _bootstrap(repo, "lx")
    fence = capture_current_decision_projection_fence(repo, accounts=("lx",))
    validations = 0
    original_validate = current_decision_combo.validate_combo_identity

    def counted_validate(identity: dict[str, object]):
        nonlocal validations
        validations += 1
        return original_validate(identity)

    monkeypatch.setattr(
        current_decision_combo,
        "validate_combo_identity",
        counted_validate,
    )
    statements: list[str] = []
    with repo._connect() as conn:  # noqa: SLF001 - transaction owner contract
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE current_decision_input_generations "
            "SET generation=generation+1,case_generation=case_generation+1 "
            "WHERE account='lx'"
        )
        conn.set_trace_callback(statements.append)
        result = finalize_current_decision_projection(
            repo,
            fence=fence,
            updated_at_ms=11_000,
            conn=conn,
        )
        conn.set_trace_callback(None)
        conn.rollback()

    assert result["statuses"] == {"lx": "published"}
    assert validations == 0
    assert not any(
        "FROM STRATEGY_GROUP_IDENTITIES" in statement.upper()
        for statement in statements
    )


def test_fence_capture_reads_metadata_without_payload_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    _bootstrap(repo, "lx")
    statements: list[str] = []
    original_connect = repo._connect  # noqa: SLF001 - SQL boundary proof

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(repo, "_connect", traced_connect)
    monkeypatch.setattr(
        "src.application.ledger.current_decision_runtime._decode_projection_row_payload",
        lambda *_args, **_kwargs: pytest.fail("fence decoded payload"),
    )
    fence = capture_current_decision_projection_fence(repo, accounts=("lx",))
    projection_reads = [
        " ".join(statement.lower().split())
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "current_decision_projections" in statement.lower()
    ]
    assert fence.accounts[0].clean_at_start is True
    assert len(projection_reads) == 1
    assert "payload_json" not in projection_reads[0]
    assert not projection_reads[0].startswith("select *")


def test_finalizer_defers_absent_and_preexisting_dirty_but_publishes_local_change(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, accounts=("absent", "dirty", "local"))
    _bootstrap(repo, "dirty")
    _bootstrap(repo, "local")
    with repo._connect() as conn:  # noqa: SLF001 - create a preexisting mismatch
        conn.execute(
            """
            UPDATE current_decision_input_generations
            SET generation = generation + 1,
                timing_generation = timing_generation + 1
            WHERE account = 'dirty'
            """
        )
    fence = capture_current_decision_projection_fence(
        repo,
        accounts=("absent", "dirty", "local"),
    )
    statements: list[str] = []
    with repo._connect() as conn:  # noqa: SLF001 - publication transaction proof
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE current_decision_input_generations
            SET generation = generation + 1,
                timing_generation = timing_generation + 1
            WHERE account = 'local'
            """
        )
        conn.set_trace_callback(statements.append)
        result = finalize_current_decision_projection(
            repo,
            fence=fence,
            updated_at_ms=12_000,
            conn=conn,
        )
        conn.set_trace_callback(None)
        conn.rollback()

    assert result["statuses"] == {
        "absent": "not_initialized",
        "dirty": "preexisting_dirty",
        "local": "published",
    }
    assert result["published_accounts"] == ["local"]
    assert result["projection_dml_count"] == 1
    assert sum(
        statement.lstrip().upper().startswith("INSERT INTO CURRENT_DECISION_PROJECTIONS")
        for statement in statements
    ) == 1


def test_incremental_owner_fact_surfaces_match_the_frozen_matrix() -> None:
    expectations = {
        writer.rebuild_position_lots_from_trade_events: (
            "run_position_projection_in_transaction",
            "defer_current_decision_projection",
        ),
        writer.persist_trade_event_object: (
            "run_position_projection_in_transaction",
            "_finish_trade_event_decision_projection",
        ),
        writer.persist_trade_event_with_combo_identity: (
            "existing_identity",
            "projected_lots",
            "_finish_trade_event_decision_projection",
        ),
        writer.adopt_existing_combo_identity_atomically: (
            "current_lots",
            "identity",
            "finalize_current_decision_projection",
        ),
        writer.persist_trade_event_objects_atomically: (
            "case_update",
            "allocation_rows",
            "run_position_projection_in_transaction",
            "_finish_trade_event_decision_projection",
            "_finish_lifecycle_decision_projection",
        ),
        manual_trades.persist_manual_adjust_events: (
            "current_by_record_id",
            "run_position_projection_in_transaction",
            "_finish_trade_event_decision_projection",
        ),
        combo_reconciliation.adopt_post_trade_combo_pair: (
            "_validate_inference_against_current_ledger",
            "identity",
            "_finish_trade_event_decision_projection",
        ),
        combo_reconciliation.supersede_post_trade_combo_pair: (
            "membership_after",
            "run_position_projection_in_transaction",
            "_finish_trade_event_decision_projection",
        ),
        writer.apply_lifecycle_allocation_atomically: (
            "get_trade_lifecycle_case",
            "derived_summary",
            "admission",
        ),
        writer.accept_option_close_evidence_atomically: (
            "get_trade_lifecycle_case",
            "existing_evidence",
            "contract_identity",
        ),
        writer.discover_expired_lifecycle_cases_atomically: (
            "position_lots",
            "existing_cases",
            "target_owner",
        ),
        writer.record_lifecycle_evidence_issue_atomically: (
            "get_trade_lifecycle_case",
            "derived_summary",
            "admission",
        ),
        writer.record_lifecycle_observation_attempt_atomically: (
            "get_trade_lifecycle_case",
            "evidence_created",
            "admission",
        ),
        writer.advance_lifecycle_case_state_atomically: (
            "get_trade_lifecycle_case",
            "derived_summary",
            "admission",
        ),
        lifecycle_timing.bind_lifecycle_timing_policy: (
            "record_lifecycle_timing_policy",
            "policy",
        ),
        workflows._execute_assigned_stock_sale: (  # noqa: SLF001 - owner inventory
            "before_report",
            "after_report",
            "sale_event",
            "record_assigned_stock_event",
        ),
    }
    for owner, required_facts in expectations.items():
        source = inspect.getsource(owner)
        assert all(fact in source for fact in required_facts), owner.__name__

    batch_source = inspect.getsource(writer.persist_trade_event_objects_atomically)
    assert "list_trade_lifecycle_allocations" not in batch_source
    assert "_effective_void_target_ids" not in batch_source
    assigned_sale_source = inspect.getsource(workflows._execute_assigned_stock_sale)  # noqa: SLF001
    assert "list_position_lot_snapshots" not in assigned_sale_source
    assert "read_current_position_projection" in assigned_sale_source


def test_lifecycle_allocation_delta_uses_only_prior_fact_and_created_rows() -> None:
    updated = writer._lifecycle_resolution_after_allocations(  # noqa: SLF001
        _case_fact(),
        allocations=[
            {
                "target_lot_id": "lot-lx",
                "contracts_allocated": 1,
                "terminal_type": "assignment",
            }
        ],
        created_flags=[True],
    )

    assert updated == {
        "resolved_contracts_by_lot": {"lot-lx": 1},
        "remaining_contracts_by_lot": {"lot-lx": 0},
        "resolved_contracts_by_terminal_type": {"assignment": 1},
        "requested_reservations_by_lot": {},
        "effective_reservations_by_lot": {},
    }


def test_trade_event_owner_publishes_global_fanout_and_duplicate_is_zero_write(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, accounts=("lx", "sy"))
    for account in ("lx", "sy"):
        _bootstrap(repo, account)

    result = writer.persist_trade_event_object(
        repo,
        _event(
            "open-global-owner",
            account="lx",
            symbol="MSFT",
            event_time_ms=2_000,
            lot_id="lot-global-owner",
        ),
    )
    assert result.details["decision_projection"]["published_accounts"] == [
        "lx",
        "sy",
    ]
    before = {
        account: repo.read_current_decision_storage_state(account)
        for account in ("lx", "sy")
    }

    repeated = writer.persist_trade_event_object(
        repo,
        _event(
            "open-global-owner",
            account="lx",
            symbol="MSFT",
            event_time_ms=2_000,
            lot_id="lot-global-owner",
        ),
    )
    assert repeated.created is False
    assert repeated.details["decision_projection"]["projection_dml_count"] == 0
    assert {
        account: repo.read_current_decision_storage_state(account)
        for account in ("lx", "sy")
    } == before


def test_global_event_fence_includes_a_first_lifecycle_account(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "empty-ledger.sqlite3")
    with repo._connect() as conn:  # noqa: SLF001 - transaction fence proof
        fence = capture_trade_event_decision_projection_fence(
            repo,
            conn=conn,
            account="lx",
        )

    assert fence is not None
    assert [item.account for item in fence.accounts] == ["lx"]
    assert fence.accounts[0].projection_present is False


def test_assignment_event_owner_advances_compact_stock_without_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    _bootstrap(repo, "lx")
    monkeypatch.setattr(
        repo,
        "list_assigned_stock_events",
        lambda **_kwargs: pytest.fail("assigned-stock history was read"),
    )
    event = TradeEvent(
        event_id="assignment-owner",
        event_type="assignment",
        event_time_ms=2_000,
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=0,
        currency="USD",
        source="test",
        multiplier=100,
        target_lot_id="lot-lx",
        raw_payload={
            "record_id": "lot-lx",
            "target_lot_id": "lot-lx",
            "stock_settlement": {
                "side": "buy",
                "shares": 100,
                "price": 100,
                "fees": 1,
                "event_time_ms": 2_000,
            },
        },
    )

    result = writer.persist_trade_event_object(repo, event)
    assigned = read_current_decision_projection(
        repo,
        account="lx",
        now_ms=3_000,
    )["payload"]["assigned_stock"]
    assert result.details["decision_projection"]["statuses"] == {
        "lx": "published"
    }
    assert assigned["lots"][0]["stock_lot_id"] == (
        "assigned-stock-assignment-owner"
    )
    assert assigned["lots"][0]["remaining_cost_basis"] == "10001"


@pytest.mark.parametrize(
    ("stock_time_ms", "event_time_ms", "opened_at_ms", "message"),
    ((999, 999, 1_000, "backdated"), (0, 2_000, 1_000, "integer >= 1")),
)
def test_trade_event_adapter_rejects_invalid_settlement_time(
    stock_time_ms: int,
    event_time_ms: int,
    opened_at_ms: int,
    message: str,
) -> None:
    transition = _buy_transition()
    prior = empty_assigned_stock_fact("lx")
    event = TradeEvent(
        event_id="assignment-backdated",
        event_type="assignment",
        event_time_ms=event_time_ms,
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=0,
        currency="USD",
        source="test",
        multiplier=100,
        target_lot_id="lot-source",
        raw_payload={
            "stock_settlement": {
                "side": "buy",
                "shares": 100,
                "price": 100,
                "fees": 0,
                "event_time_ms": stock_time_ms,
            }
        },
    )
    final_lot = _final_option_lot(transition)
    final_lot["fields"]["opened_at"] = opened_at_ms
    with pytest.raises(CurrentDecisionProjectionError, match=message):
        advance_assigned_stock_fact_for_trade_events(
            prior,
            event_mutations=((event, True),),
            current_position_lots=[final_lot],
        )


def test_assigned_stock_sale_owner_publishes_partial_full_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    transition = _buy_transition()
    assigned = update_assigned_stock_fact(
        empty_assigned_stock_fact("lx"),
        transition=transition,
        current_position_lots=[_final_option_lot(transition)],
    )
    payload = build_current_decision_projection(
        repo,
        account="lx",
        updated_at_ms=10_000,
        assigned_stock_after=assigned,
        all_quality_case_facts=[],
    )
    repo.upsert_current_decision_projection(current_decision_projection_row(payload))
    lot = assigned["lots"][0]

    partial_after = update_assigned_stock_fact(
        assigned,
        transition={
            "kind": "assigned_stock_sale",
            "stock_event_id": "sale-owner-a",
            "stock_lot_id": lot["stock_lot_id"],
            "shares": 40,
            "trade_time_ms": 3_000,
            "lot_after": _sale_after(lot, event_id="sale-owner-a", shares=40),
        },
        current_position_lots=[],
    )
    sale_a = {
        "stock_event_id": "sale-owner-a",
        "event_type": "sale",
        "target_stock_lot_id": lot["stock_lot_id"],
        "account": "lx",
        "shares": 40,
        "trade_time_ms": 3_000,
    }
    first = writer.record_assigned_stock_event_atomically(
        repo,
        sale_event=sale_a,
        assigned_stock_after=partial_after,
    )
    assert first["created"] is True
    assert first["decision_projection"]["statuses"] == {"lx": "published"}
    before_duplicate = repo.read_current_decision_storage_state("lx")
    assert writer.record_assigned_stock_event_atomically(
        repo,
        sale_event=sale_a,
        assigned_stock_after=partial_after,
    )["decision_projection"]["projection_dml_count"] == 0
    assert repo.read_current_decision_storage_state("lx") == before_duplicate

    rollback_after = update_assigned_stock_fact(
        partial_after,
        transition={
            "kind": "assigned_stock_sale",
            "stock_event_id": "sale-owner-rollback",
            "stock_lot_id": lot["stock_lot_id"],
            "shares": 10,
            "trade_time_ms": 3_500,
            "lot_after": _sale_after(
                partial_after["lots"][0],
                event_id="sale-owner-rollback",
                shares=10,
            ),
        },
        current_position_lots=[],
    )
    rollback_event = {
        **sale_a,
        "stock_event_id": "sale-owner-rollback",
        "shares": 10,
        "trade_time_ms": 3_500,
    }
    with monkeypatch.context() as patch:
        patch.setattr(
            repo,
            "upsert_current_decision_projection",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("projection failed")
            ),
        )
        with pytest.raises(RuntimeError, match="projection failed"):
            writer.record_assigned_stock_event_atomically(
                repo,
                sale_event=rollback_event,
                assigned_stock_after=rollback_after,
            )
    assert all(
        row["stock_event_id"] != "sale-owner-rollback"
        for row in repo.list_assigned_stock_events()
    )

    partial_lot = partial_after["lots"][0]
    full_after = update_assigned_stock_fact(
        partial_after,
        transition={
            "kind": "assigned_stock_sale",
            "stock_event_id": "sale-owner-b",
            "stock_lot_id": partial_lot["stock_lot_id"],
            "shares": 60,
            "trade_time_ms": 4_000,
            "lot_after": None,
        },
        current_position_lots=[],
    )
    sale_b = {
        "stock_event_id": "sale-owner-b",
        "event_type": "sale",
        "target_stock_lot_id": partial_lot["stock_lot_id"],
        "account": "lx",
        "shares": 60,
        "trade_time_ms": 4_000,
    }
    writer.record_assigned_stock_event_atomically(
        repo,
        sale_event=sale_b,
        assigned_stock_after=full_after,
    )
    assert read_current_decision_projection(
        repo,
        account="lx",
        now_ms=5_000,
    )["payload"]["assigned_stock"]["lots"] == []
