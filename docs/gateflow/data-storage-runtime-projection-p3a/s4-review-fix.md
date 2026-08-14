# S4 DeepReview Fix

- Gate: `fix`
- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S4`
- Review: `docs/reviews/code-review-20260814-093216.md`
- Status: fixed; aggregate re-review passed
- Re-review: `docs/reviews/code-review-20260814-094412.md`
- Updated: `2026-08-14 09:37:51 +0800`

## DR-S4-01 — accepted — fixed

The shared append-preview helper now checks current-history diagnostics before
candidate diagnostics. Invalid current history keeps the existing
`ledger_shadow_invalid` contract with separate `import_errors` and
`projection_errors`; invalid candidate events keep the existing open, close,
or adjust `*_projection_invalid` code and `errors` payload.

The fix stays at the shared preflight boundary and adds no new public type or
runtime path.

## Verification

- Focused preflight/runtime suite: `54 passed`.
- S4 and adjacent ledger/lifecycle suite: `298 passed`.
- Ruff on all S4 Python files: passed.
- Compileall for ledger application/domain packages: passed.
- `git diff --check`: passed.
- Added full-fallback and checkpoint-enabled candidate-error parity coverage.
- Frozen current-history projection-only and import-plus-projection diagnostic
  classification.
