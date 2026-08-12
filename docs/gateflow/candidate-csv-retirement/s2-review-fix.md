# Gateflow Fix Artifact — S2 DeepReview

- Gate: `fix`
- Work unit: `candidate-csv-retirement`
- Slice: `S2`
- Initial review: `docs/reviews/code-review-20260812-130109.md`
- Status: complete

## Finding decisions

- `S2-CR-01`: accepted and fixed. Account and known-market filters now scope the candidate-evidence
  classifications before counts, availability, and strict replay authority are recomputed. Unknown-market
  unsupported evidence remains in scope and fails closed.
- `S2-CR-02`: accepted and fixed. Combo Funding Put publication re-reads and validates the current dataset manifest
  under the write lock; consumption validates dataset integrity, receipt schema/source hashes/counts, and exact
  manifest facet binding under one read lock, and returns the validated rows without reopening the JSONL.

## Regression coverage

- Mixed `lx=supported` / `sy=unsupported` run-window coverage is strict only when the request explicitly scopes to
  `lx`; the unscoped request remains incomplete.
- Dataset coverage follows the same account/market scoping rule.
- A known other-market classification can be excluded; an unsupported classification with unknown market remains
  fail-closed.
- Receipt/facet tamper, JSONL+receipt synchronized tamper, stale integrity, canonical-path enforcement, accepted-only
  evaluation, and idempotent projection publication are covered.

## Verification

- Focused review-fix tests: `47 passed`.
- Complete S2 focused suite: `197 passed`.
- Ruff over the changed review-fix files: pass.
- `git diff --check`: pass.
