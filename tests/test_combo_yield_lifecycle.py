from pathlib import Path

import src.application.ledger.repository as ledger_repository

from domain.domain.combo_yield_lifecycle import build_full_group_lifecycle, build_option_group_inventory


def _lot(record_id: str, *, option_type: str, side: str, opened: int, open_count: int, expiration: str, group_id: str | None, structure: str | None = "same_expiry", structure_mode: str | None = None) -> dict:
    snapshot: dict = {}
    if structure is not None:
        snapshot["expiry_structure"] = structure
    if structure_mode is not None:
        snapshot["structure_mode"] = structure_mode
    return {
        "record_id": record_id,
        "account": "lx",
        "symbol": "PDD",
        "option_type": option_type,
        "side": side,
        "contracts": opened,
        "contracts_open": open_count,
        "contracts_closed": opened - open_count,
        "expiration_ymd": expiration,
        "strategy": "combo_yield",
        "leg_role": "sell_put" if option_type == "put" else "enhancement_call",
        "strategy_group_id": group_id,
        "strategy_snapshot": snapshot,
    }


def test_option_group_inventory_aggregates_partial_lots_and_quantities() -> None:
    group_id = "combo_yield:lx:pair-1"
    rows = [
        _lot("put-1", option_type="put", side="short", opened=2, open_count=2, expiration="2026-09-18", group_id=group_id),
        _lot("call-1", option_type="call", side="long", opened=1, open_count=1, expiration="2026-09-18", group_id=group_id),
        _lot("call-2", option_type="call", side="long", opened=1, open_count=1, expiration="2026-09-18", group_id=group_id),
    ]

    group = build_option_group_inventory(rows)[0]

    assert group["put_contracts_open"] == 2
    assert group["call_contracts_open"] == 2
    assert group["summary_classification"] == "active_combo"
    assert group["inventory_issues"] == []
    assert group["evidence_scope"] == "option_lots"


def test_option_group_inventory_marks_quantity_and_identity_issues_review_required() -> None:
    group_id = "combo_yield:lx:pair-2"
    mismatch = build_option_group_inventory(
        [
            _lot("put-1", option_type="put", side="short", opened=2, open_count=2, expiration="2026-09-18", group_id=group_id),
            _lot("call-1", option_type="call", side="long", opened=1, open_count=1, expiration="2026-09-18", group_id=group_id),
        ]
    )[0]
    missing = build_option_group_inventory(
        [_lot("call-missing", option_type="call", side="long", opened=1, open_count=1, expiration="2026-09-18", group_id=None)]
    )[0]

    assert mismatch["summary_classification"] == "review_required"
    assert "open_quantity_mismatch" in mismatch["inventory_issues"]
    assert missing["summary_classification"] == "review_required"
    assert missing["inventory_issues"] == ["missing_group_identity"]


def test_option_group_inventory_classifies_missing_and_residual_legs() -> None:
    put_only = build_option_group_inventory(
        [_lot("put", option_type="put", side="short", opened=1, open_count=1, expiration="2026-08-21", group_id="combo_yield:lx:put")]
    )[0]
    call_only = build_option_group_inventory(
        [_lot("call", option_type="call", side="long", opened=1, open_count=1, expiration="2026-09-18", group_id="combo_yield:lx:call")]
    )[0]

    assert put_only["summary_classification"] == "missing_call"
    assert call_only["summary_classification"] == "residual_call"


def test_option_group_inventory_fails_closed_on_malformed_quantity_and_missing_evidence() -> None:
    malformed = _lot(
        "bad-call",
        option_type="call",
        side="long",
        opened=1,
        open_count=1,
        expiration="2026-09-18",
        group_id="combo_yield:lx:bad",
    )
    malformed["contracts_open"] = "not-a-number"
    missing_expiration = _lot(
        "missing-exp",
        option_type="put",
        side="short",
        opened=1,
        open_count=1,
        expiration="",
        group_id="combo_yield:lx:missing-exp",
    )

    malformed_group = build_option_group_inventory([malformed])[0]
    missing_group = build_option_group_inventory([missing_expiration])[0]

    assert malformed_group["summary_classification"] == "review_required"
    assert "invalid_contract_quantity" in malformed_group["inventory_issues"]
    assert missing_group["summary_classification"] == "review_required"
    assert "put_expiration_missing" in missing_group["inventory_issues"]


def test_option_group_inventory_fails_closed_on_legacy_staggered_structure() -> None:
    group_id = "combo_yield:lx:pair-legacy"
    rows = [
        _lot("put-1", option_type="put", side="short", opened=1, open_count=1, expiration="2026-08-21", group_id=group_id, structure="diagonal"),
        _lot("call-1", option_type="call", side="long", opened=1, open_count=1, expiration="2026-09-18", group_id=group_id, structure="diagonal"),
    ]

    group = build_option_group_inventory(rows)[0]

    assert group["expiry_structure"] == "diagonal"
    assert group["summary_classification"] == "review_required"
    assert "unsupported_expiry_structure" in group["inventory_issues"]
    assert "same_expiry_mismatch" not in group["inventory_issues"]


def test_option_group_inventory_fails_closed_on_staggered_structure_mode_same_expiry() -> None:
    group_id = "combo_yield:lx:pair-legacy-mode"
    rows = [
        _lot("put-1", option_type="put", side="short", opened=1, open_count=1, expiration="2026-08-21", group_id=group_id, structure=None, structure_mode="staggered_expiry_pair"),
        _lot("call-1", option_type="call", side="long", opened=1, open_count=1, expiration="2026-08-21", group_id=group_id, structure=None, structure_mode="staggered_expiry_pair"),
    ]

    group = build_option_group_inventory(rows)[0]

    assert group["expiry_structure"] == "unknown"
    assert group["summary_classification"] == "review_required"
    assert "unsupported_expiry_structure" in group["inventory_issues"]


def test_manual_open_preview_projection_restart_reconstructs_same_expiry_group(tmp_path: Path) -> None:
    from src.application.positions.context_builder import build_context
    from src.application.positions.workflows import execute_manual_open

    db_path = tmp_path / "option_positions.sqlite3"
    repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    group_id = "combo_yield:lx:combo_yield|PDD|PDD_P80_AUG|PDD_C100_SEP"

    def snapshot(leg_role: str) -> dict:
        return {
            "strategy": "combo_yield",
            "leg_role": leg_role,
            "strategy_group_id": group_id,
            "expiry_structure": "same_expiry",
            "yield_enhancement_mode": "income_upside",
        }

    preview = execute_manual_open(
        repo,
        broker="富途",
        account="lx",
        symbol="PDD",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=80.0,
        multiplier=100,
        expiration_ymd="2026-09-18",
        premium_per_share=1.0,
        underlying_share_locked=None,
        note=None,
        opened_at_ms=1000,
        strategy_snapshot=snapshot("sell_put"),
        dry_run=True,
        request_id="combo-yield-preview-put",
    )
    assert preview["fields"]["strategy_group_id"] == group_id
    assert preview["fields"]["strategy"] == "combo_yield"

    for option_type, side, strike, expiration, role, opened_at_ms in (
        ("put", "short", 80.0, "2026-09-18", "sell_put", 1000),
        ("call", "long", 100.0, "2026-09-18", "enhancement_call", 2000),
    ):
        result = execute_manual_open(
            repo,
            broker="富途",
            account="lx",
            symbol="PDD",
            option_type=option_type,
            side=side,
            contracts=1,
            currency="USD",
            strike=strike,
            multiplier=100,
            expiration_ymd=expiration,
            premium_per_share=1.0,
            underlying_share_locked=None,
            note=None,
            opened_at_ms=opened_at_ms,
            strategy_snapshot=snapshot(role),
            dry_run=False,
            request_id=f"combo-yield-{role}",
        )
        assert result["fields"]["strategy_group_id"] == group_id

    restarted = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    lots = restarted.list_position_lots()
    assert len(lots) == 2
    assert {lot["fields"]["strategy_group_id"] for lot in lots} == {group_id}
    groups = build_context(lots, broker="富途", account="lx")["combo_yield_groups"]
    assert groups[0]["summary_classification"] == "active_combo"
    assert groups[0]["put_expiration"] == "2026-09-18"
    assert groups[0]["call_expiration"] == "2026-09-18"


def test_manual_open_invalidates_position_context_cache(tmp_path: Path) -> None:
    from src.application.positions.workflows import execute_manual_open

    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
    cache_path = (
        tmp_path
        / "output_shared"
        / "state"
        / "option_positions_context.json"
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text('{"stale": true}', encoding="utf-8")

    result = execute_manual_open(
        repo,
        broker="富途",
        account="lx",
        symbol="NVDA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100.0,
        multiplier=100,
        expiration_ymd="2027-08-21",
        premium_per_share=2.0,
        underlying_share_locked=None,
        note=None,
        dry_run=False,
        request_id="cache-invalidation-open-001",
    )

    assert result["context_cache_invalidation"]["ok"] is True
    assert not cache_path.exists()


def test_full_group_lifecycle_classifies_residual_and_assignment_states() -> None:
    group_id = "combo_yield:lx:pair-full"
    residual_options = build_option_group_inventory(
        [
            _lot("put", option_type="put", side="short", opened=1, open_count=0, expiration="2026-09-18", group_id=group_id),
            _lot("call", option_type="call", side="long", opened=1, open_count=1, expiration="2026-09-18", group_id=group_id),
        ]
    )
    residual = build_full_group_lifecycle(residual_options)[0]
    assigned = build_full_group_lifecycle(
        residual_options,
        assigned_stock_lots=[
            {
                "stock_lot_id": "stock-1",
                "strategy_group_id": group_id,
                "account": "lx",
                "symbol": "PDD",
                "shares_opened": 100,
                "shares_remaining": 100,
                "shares_sold": 0,
            }
        ],
        assignment_events=[
            {
                "event_id": "assign-1",
                "strategy_group_id": group_id,
                "contracts": 1,
                "stock_settlement_valid": True,
            }
        ],
    )[0]

    assert residual["summary_classification"] == "residual_call"
    assert residual["residual_call_contracts"] == 1
    assert assigned["summary_classification"] == "assigned_stock_with_residual_call"
    assert assigned["assigned_contracts"] == 1
    assert assigned["assigned_shares_remaining"] == 100


def test_full_group_lifecycle_reconciles_partial_assignment_quantities() -> None:
    group_id = "combo_yield:lx:pair-partial"
    option_groups = build_option_group_inventory(
        [
            _lot("put", option_type="put", side="short", opened=2, open_count=1, expiration="2026-09-18", group_id=group_id),
            _lot("call", option_type="call", side="long", opened=2, open_count=2, expiration="2026-09-18", group_id=group_id),
        ]
    )
    lifecycle = build_full_group_lifecycle(
        option_groups,
        assigned_stock_lots=[
            {
                "stock_lot_id": "stock-partial",
                "strategy_group_id": group_id,
                "account": "lx",
                "symbol": "PDD",
                "shares_opened": 100,
                "shares_remaining": 100,
                "shares_sold": 0,
            }
        ],
        assignment_events=[
            {
                "event_id": "assign-partial",
                "strategy_group_id": group_id,
                "contracts": 1,
                "stock_settlement_valid": True,
            }
        ],
    )[0]

    assert lifecycle["summary_classification"] == "assigned_stock_with_residual_call"
    assert lifecycle["put_contracts_open"] == 1
    assert lifecycle["call_contracts_open"] == 2
    assert lifecycle["residual_call_contracts"] == 1
    assert lifecycle["lifecycle_issues"] == []


def test_full_group_lifecycle_keeps_assignment_history_after_stock_sale() -> None:
    group_id = "combo_yield:lx:pair-closed"
    option_groups = build_option_group_inventory(
        [
            _lot("put", option_type="put", side="short", opened=1, open_count=0, expiration="2026-09-18", group_id=group_id),
            _lot("call", option_type="call", side="long", opened=1, open_count=0, expiration="2026-09-18", group_id=group_id),
        ]
    )
    lifecycle = build_full_group_lifecycle(
        option_groups,
        assigned_stock_lots=[
            {
                "stock_lot_id": "stock-sold",
                "strategy_group_id": group_id,
                "account": "lx",
                "symbol": "PDD",
                "shares_opened": 100,
                "shares_remaining": 0,
                "shares_sold": 100,
            }
        ],
        assignment_events=[
            {
                "event_id": "assign-closed",
                "strategy_group_id": group_id,
                "contracts": 1,
                "stock_settlement_valid": True,
            }
        ],
    )[0]

    assert lifecycle["summary_classification"] == "closed"
    assert lifecycle["assigned_shares_opened"] == 100
    assert lifecycle["assigned_shares_sold"] == 100
    assert lifecycle["assignment_event_ids"] == ["assign-closed"]
    assert lifecycle["assigned_stock_lot_ids"] == ["stock-sold"]


def test_full_group_lifecycle_fails_closed_when_assignment_settlement_missing() -> None:
    group_id = "combo_yield:lx:pair-missing-settlement"
    option_groups = build_option_group_inventory(
        [_lot("call", option_type="call", side="long", opened=1, open_count=1, expiration="2026-09-18", group_id=group_id)]
    )
    lifecycle = build_full_group_lifecycle(
        option_groups,
        assignment_events=[
            {
                "event_id": "assign-missing",
                "strategy_group_id": group_id,
                "contracts": 1,
                "stock_settlement_valid": False,
            }
        ],
    )[0]

    assert lifecycle["summary_classification"] == "review_required"
    assert "missing_assignment_settlement" in lifecycle["lifecycle_issues"]


def test_full_group_lifecycle_classifies_assigned_stock_without_open_call() -> None:
    group_id = "combo_yield:lx:assigned-stock-only"
    option_groups = build_option_group_inventory(
        [
            _lot("put", option_type="put", side="short", opened=1, open_count=0, expiration="2026-09-18", group_id=group_id),
            _lot("call", option_type="call", side="long", opened=1, open_count=0, expiration="2026-09-18", group_id=group_id),
        ]
    )
    lifecycle = build_full_group_lifecycle(
        option_groups,
        assigned_stock_lots=[
            {
                "stock_lot_id": "assigned-only",
                "strategy_group_id": group_id,
                "account": "lx",
                "symbol": "PDD",
                "shares_opened": 100,
                "shares_remaining": 100,
                "shares_sold": 0,
            }
        ],
        assignment_events=[
            {
                "event_id": "assign-only",
                "strategy_group_id": group_id,
                "contracts": 1,
                "stock_settlement_valid": True,
            }
        ],
    )[0]

    assert lifecycle["summary_classification"] == "assigned_stock_only"
    assert lifecycle["residual_call_contracts"] == 0
