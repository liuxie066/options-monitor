from __future__ import annotations

from domain.domain.ledger.events import LedgerDiagnostic
from domain.domain.ledger.lots import PositionLot


def check_position_lot_invariants(lots: list[PositionLot]) -> list[LedgerDiagnostic]:
    diagnostics: list[LedgerDiagnostic] = []
    seen_lot_ids: set[str] = set()
    for lot in lots:
        if lot.lot_id in seen_lot_ids:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=lot.last_event_id,
                    severity="error",
                    code="duplicate_lot_id",
                    message="lot_id must be unique",
                    details={"lot_id": lot.lot_id},
                )
            )
        seen_lot_ids.add(lot.lot_id)
        if lot.contracts_open < 0:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=lot.last_event_id,
                    severity="error",
                    code="negative_contracts_open",
                    message="contracts_open must be >= 0",
                    details={"lot_id": lot.lot_id, "contracts_open": lot.contracts_open},
                )
            )
        if lot.contracts_closed < 0:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=lot.last_event_id,
                    severity="error",
                    code="negative_contracts_closed",
                    message="contracts_closed must be >= 0",
                    details={"lot_id": lot.lot_id, "contracts_closed": lot.contracts_closed},
                )
            )
        if lot.contracts_closed > lot.contracts_opened:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=lot.last_event_id,
                    severity="error",
                    code="contracts_closed_exceeds_opened",
                    message="contracts_closed must be <= contracts_opened",
                    details={
                        "lot_id": lot.lot_id,
                        "contracts_closed": lot.contracts_closed,
                        "contracts_opened": lot.contracts_opened,
                    },
                )
            )
        if lot.contracts_open + lot.contracts_closed != lot.contracts_opened:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=lot.last_event_id,
                    severity="error",
                    code="contracts_balance_mismatch",
                    message="contracts_open + contracts_closed must equal contracts_opened",
                    details={
                        "lot_id": lot.lot_id,
                        "contracts_open": lot.contracts_open,
                        "contracts_closed": lot.contracts_closed,
                        "contracts_opened": lot.contracts_opened,
                    },
                )
            )
        expected_status = "close" if lot.contracts_open == 0 else "open"
        if lot.status != expected_status:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=lot.last_event_id,
                    severity="error",
                    code="status_quantity_mismatch",
                    message="lot status must match contracts_open",
                    details={"lot_id": lot.lot_id, "status": lot.status, "expected_status": expected_status},
                )
            )
    return diagnostics
