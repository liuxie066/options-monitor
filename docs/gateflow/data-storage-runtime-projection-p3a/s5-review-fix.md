# Gateflow S5 Review Fix — Migration and Acceptance Boundaries

- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S5`
- Gate: `fix`
- Date: 2026-08-14
- Base: `S4@75d5dcc1`
- Status: fixed; final aggregate re-review passed
- Review artifact: `docs/reviews/code-review-20260814-111520.md`

## Finding decisions and fixes

### DR-S5-01 — accepted — fixed

Verification can no longer bind dirty production source to the previous Git
commit. `_source_commit()` checks tracked, staged, and untracked changes under
`domain/`, `src/`, and `scripts/`; dirty or unavailable source adds
`source_commit_unavailable` and prevents pass/ready evidence.

### DR-S5-02 — accepted — fixed

Migration status now checks the legacy table/column contract before selecting
normalized `position_lots.account` or `fields_json`. Unprepared stores return
structured `not_ready` reasons instead of raising SQL errors.

### DR-S5-03 — accepted — fixed

Activation now validates nested component types and statuses, exact reference
host fingerprint equality, shadow readiness, full-oracle/runtime-shadow parity,
failure lists, both store-binding mappings, and the current clean source commit
before opening the write transaction.

### GF-S5-FS-01 — accepted — fixed

The full suite correctly rejected unclassified new full-oracle/runtime calls.
The exact AST inventory now names the one-time migration apply, read-only full
verification, and the telemetry wrapper's single event-write owner. The gate
was not weakened and continues to fail on any undeclared O(E) or write path.

## Verification

```text
S5 migration/benchmark/CLI focused set: 102 passed
ledger/research/CLI aggregate set: 297 passed
migration plus exact facade inventory: 20 passed
repository suite excluding sandbox-only localhost bind: 4734 passed, 10 skipped
localhost HTTP test outside sandbox: 1 passed
ruff: passed
compileall: passed
dependency graph: current; production_modules=577; cycles=0
git diff --check: passed
```

No live SQLite store, runtime configuration, service, notification, broker, or
deployment state was touched.
