# Decision: projection-to-repository lot writes use PositionLotRecord

Date: 2026-05-18

## Context

Business field patches and sync metadata patches had explicit contracts, but the projection publish path still passed `position_lots` rows as `list[dict]` into repository replacement.

## Decision

- `src/application/ledger/position_records.py` owns `PositionLotRecord`.
- `PublishedPositionLotProjection.lots` now contains `PositionLotRecord`.
- `SQLiteOptionPositionsRepository.replace_position_lots(...)` requires `Sequence[PositionLotRecord]` and rejects raw dict records.
- Read surfaces can still return dict records for CLI/report compatibility; this cut only constrains the write path.

## Rationale

The canonical replay chain is `trade_events -> projection -> position_lots`. The publish-to-storage boundary must not allow arbitrary row shapes to bypass the canonical lot record contract.
