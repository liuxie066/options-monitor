# Gateflow S2 Review Fix

- Gate: S2 fix
- Work unit: `lifecycle-receipt-batching`
- Input review: `docs/reviews/code-review-20260802-055541.md`
- Status: accepted findings fixed; pending re-review

## DR-S2-01 — accepted — fixed

- Classifier now evaluates unambiguous HTTP/provider/pre-I/O rejection before `command_ok -> accepted`.
- Added a regression using the actual WeChat normalizer shape: HTTP 200, `command_ok=true`, nonzero provider code now becomes `explicit_failed`.
- 4xx, transient 5xx and fallback/ambiguous cases remain pinned.

## DR-S2-02 — accepted — fixed

- CLI resolves the canonical trade-intake sources and builds a fail-closed allow-set from enabled source account/mapping values whose receipt is enabled.
- Both dry-run planner and applied dispatcher receive that allow-set.
- Empty/global-disabled allow-sets return an idle/no-write result before batching or sending.
- Added enabled `lx` plus disabled `sy`, and global receipt-disabled regressions; disabled rows remain pending and unbound.

## DR-S2-03 — accepted — fixed

- Renderer now collapses members first and delegates to the existing single-case renderer whenever exactly one representative remains.
- Added exact message equality for two transitions of one case.

## DR-S2-04 — accepted — fixed

- CLI write evidence now requires an actual returned batch, a newly created batch, or a nonzero stale-recovery mutation.
- Added applied empty-ledger coverage proving idle returns `write_applied=false`.

## Validation

- Focused S2/S1 suite after fixes: `114 passed`.
- Ruff: pass.
- Python compile checks: pass.
- `git diff --check`: pass.
