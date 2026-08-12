# Gateflow Fix Artifact — S1 DeepReview

- Gate: `fix`
- Work unit: `candidate-csv-retirement`
- Slice: `S1`
- Initial review: `docs/reviews/code-review-20260812-094647.md`
- Status: complete

## Finding decisions

- `S1-CR-01`: accepted and fixed. Each owner snapshot must match the unique market of its v2 status scopes.
- `S1-CR-02`: accepted and fixed. Every completed Combo scope must supply exactly one typed evidence envelope;
  failed or unavailable scopes may remain status-only.
- `S1-CR-03`: accepted and fixed. Funding Put, pair diagnostic, rank, and selected rows are bound to the sealed
  run, account, symbol scopes, and stable leg-derived pair identity.
- `S1-CR-04`: accepted and fixed. Terminal reason parity is exact for completed and non-completed scopes.

## Additional hardening during re-review

- CC+LP selected pairs are bound to run/account/scope and stable leg identity.
- v2 completed candidate counts must be non-negative integers; half-present quote bindings are invalid.
- A non-completed scope cannot carry selected rows in a terminal bundle.
- Deterministic opening rejection projection was added beside the snapshot owner.

## Verification

- Focused contract tests: `54 passed`.
- Expanded S1 consumer and orchestration tests: `262 passed`.
- Ruff: pass.
- Diff whitespace check: pass.
