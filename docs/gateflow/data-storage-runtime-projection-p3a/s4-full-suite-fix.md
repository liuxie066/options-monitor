# S4 Full-Suite Fix

- Gate: `fix`
- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S4`
- Trigger: first full-suite run after aggregate re-review
- Updated: `2026-08-14 09:49:33 +0800`
- Status: fixed; final S4 re-review passed
- Final re-review: `docs/reviews/code-review-20260814-095029.md`

## FS-S4-01 — accepted — fixed

The indexed active-lot and exact-lot resolver paths bypassed custom repository
subclasses that intentionally override the public `list_position_lots()` read
contract. This broke two fail-closed compatibility tests.

The bounded readers are now used only by the canonical
`SQLiteOptionPositionsRepository`. Adapters and subclasses retain the existing
public read path. Production SQLite writers still use the indexed active/PK
queries; no full event-history replay was restored.

## FS-S4-02 — accepted — fixed

The S1 source inventory test still required each full writer module to import
`publish_full_position_projection` directly. S4 intentionally replaces that
ownership with `run_position_projection_in_transaction`; the inventory now
freezes the new shared runtime boundary and still forbids global lot-table
replacement.

## FS-S4-03 — accepted — fixed

The import changes made the two generated dependency-graph artifacts stale.
They were regenerated mechanically. Check mode reports 576 production modules
and zero production cycles.

## Verification

- Direct full-suite regressions and dependency graph: `6 passed`.
- Expanded S4/ledger/maintenance/manual-close suite: `349 passed`.
- Final full suite: `4707 passed`, `10 skipped`, with only the two unchanged
  base/environment failures described below.
- The localhost HTTP test passed outside the sandbox: `1 passed`.
- Ruff and `git diff --check`: passed.
- The separate non-ledger-to-ledger-internals failure is unchanged from base
  `5930e5ce`; all five offenders are under unmodified research files.
- The localhost HTTP test remains unavailable inside the filesystem sandbox
  because socket bind returns `EPERM`.
