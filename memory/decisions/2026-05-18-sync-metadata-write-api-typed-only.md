# Decision: sync metadata write APIs are typed-only

Date: 2026-05-18

## Context

`PositionLotSyncMetadataPatch` was introduced to separate Feishu mirror metadata from canonical lot state, but the repository and command APIs still accepted raw dict patches.

## Decision

- `record_position_lot_sync_metadata(...)` and `SQLiteOptionPositionsRepository.update_position_lot_sync_metadata(...)` now require `PositionLotSyncMetadataPatch`.
- Dict decoding remains available only as an explicit helper for raw payload or test boundaries.
- Tests construct typed metadata patches and assert business-field dicts are rejected before they can mutate `position_lots`.

## Rationale

The canonical position model should not expose generic field mutation through integration metadata paths. Typed-only writes make accidental business state mutation structurally harder.
