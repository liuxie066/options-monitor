# Decision: position lot sync metadata has an explicit patch contract

Date: 2026-05-18

## Context

After `PositionLotFields` / `PositionLotPatch` moved business lot writes into explicit contracts, the remaining ambiguous write surface was repository-level sync metadata. The old `update_position_lot_fields(record_id, fields)` name looked like a generic lot-field mutation API even though it only preserved Feishu sync metadata.

## Decision

- `src/application/ledger/sync_metadata.py` owns `PositionLotSyncMetadataPatch`.
- Feishu mirror sync builds a metadata-only patch with `build_position_lot_sync_metadata_patch`.
- `SQLiteOptionPositionsRepository` exposes `update_position_lot_sync_metadata(...)` instead of `update_position_lot_fields(...)`.
- Unsupported business fields in sync metadata patches are rejected instead of silently ignored.

## Rationale

Sync metadata is integration state, not canonical position state. Keeping this as a separate contract prevents Feishu mirror writes from becoming an accidental bypass around `trade_events -> position_lots` projection.
