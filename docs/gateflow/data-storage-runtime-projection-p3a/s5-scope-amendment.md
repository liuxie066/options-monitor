# Gateflow S5 Scope Amendment

- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S5`
- Gate: `plan`
- Date: 2026-08-14
- Status: accepted by the user's continuation confirmation before S5 code

## Required correction

S5 adds a ledger migration facade consumed by the option-position CLI and the
research benchmark. The repository's enforced import contract permits those
non-ledger modules to import ledger behavior only through
`src.application.ledger.api`. The accepted S5 file list omitted that thin
re-export file, so implementing the CLI literally would either fail the
dependency test or hide the import dynamically.

The allowed scope now includes only:

- `src/application/ledger/api.py` for thin S5 re-exports; and
- `src/application/ledger/position_projection_runtime.py` only for the
  already-accepted bounded in-process fast/full/fallback and wall/CPU status
  summaries; and
- `src/application/research/storage_baseline.py` for replacing its pre-existing
  direct ledger-internal constant import with the same public API; and
- `tests/test_position_projection_facade_inventory.py` for explicitly
  classifying the new one-time migration full-oracle/runtime calls and the
  telemetry wrapper's single event-write owner; and
- the two generated dependency-graph files if the new production module makes
  their checked inventory stale.

No command/domain behavior moves into the API, no new configuration key is
added, telemetry is not persisted per event, and no live migration or
activation authority is granted.
