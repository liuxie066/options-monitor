# Required-data Validation Slimming

## Intent

Implement the accepted Required Data validation slimming PRD without changing
strategy policy, OpenD execution, cache policy, notification policy, frozen
snapshot authority, or public CLI behavior.

The validator must answer whether the captured data is safe to consume. It must
not reject semantically equivalent OpenD results because internal lists use a
different order, and it must not report internal-contract defects as provider
data loss.

## In-scope decisions

1. Scope identity is the unordered unique key
   `(planned_request_sha256, option_type, expiration)`. `request_index` remains
   compatibility evidence but is not semantic identity.
2. Contract-code identity is set-based. Input order is not acceptance evidence.
3. Every requested snapshot code must be returned. Unexpected provider codes
   are quarantined by the existing snapshot adapter, retained as warnings, and
   do not invalidate complete requested rows.
4. A fully observed, fully filtered empty grid is `success_empty` when no exact
   strike is required. It is not a fetch failure.
5. A missing exact strike remains fail-closed as
   `required_contract_missing`.
6. Coverage evaluation returns a structured result with overall status,
   provider coverage, internal integrity, freshness evidence strength, strategy
   readiness, reason code, warnings, and details. Existing boolean functions
   remain compatibility facades.
7. Candidate/persistence validation raises reason-coded errors so
   `provider_incomplete`, `stale_data`, and `internal_contract_error` are not
   collapsed into one generic coverage message.
8. Freshness continues to use the existing trading-date/cache and 30-minute
   policy. The structured result explicitly identifies the current quote-time
   proof as `system_observed_at`; this work does not invent unavailable
   provider timestamps.

## Explicit non-goals

- No strategy threshold, ranking, liquidity, IV, Delta, bid/ask, or candidate
  policy changes.
- No attempt to prove that OpenD's option catalog itself never omits a listed
  contract.
- No new OpenD calls, retries, fallback behavior, rate-limit behavior, cache
  age, or timeout behavior.
- No notification, scheduler, account, ledger, position, release, or deployment
  behavior changes in the source work unit.
- No new persistence store, manifest version, receipt version, public command,
  or configuration key.
- No row-level quote timestamp schema. Current timestamp evidence is classified
  honestly rather than expanded.
- No broad cleanup of existing required-data modules.

## Ownership and implementation slices

### Slice A: coverage decision owner

Owner: `src/application/required_data_coverage.py`

- Add the structured evaluation result and one debug-plan evaluator.
- Match scopes and codes as sets/maps with explicit duplicate rejection.
- Distinguish provider incompleteness, exact-contract absence, identity
  mismatch, invalid row identity, internal-contract errors, and proven empty.
- Keep typed/debug boolean entry points as wrappers.

### Slice B: provider evidence acceptance

Owners:

- `src/application/opend_symbol_fetching.py`
- `src/application/required_data_fetching.py`
- `src/application/opend_symbol_outputs.py`

- Preserve request-scoped evidence while allowing equivalent ordering.
- Treat filtered-empty provider results as successful empty evidence.
- Quarantine unexpected snapshot codes already excluded from consumer rows;
  surface a warning instead of treating complete requested rows as missing.
- Convert structured coverage failures into reason-coded validation errors.
- Classify schema/hash/count/persistence contradictions as internal-contract
  errors, not OpenD incompleteness.

### Slice C: tests and operator documentation

Owners:

- `tests/test_required_data_exact_coverage.py`
- focused existing output/prefetch tests only when their public behavior changes
- `docs/AGENT_WIKI.md`
- `docs/DEPENDENCY_GRAPH.md` as the generated test-import projection only

Required regressions:

1. FUTU-shaped unsorted call expirations pass when scope/code sets match.
2. Scope and code list reordering pass.
3. Missing requested snapshot fails as `provider_incomplete`.
4. Wrong type/expiration binding fails as `scope_identity_mismatch`.
5. Unexpected provider codes are warned and excluded, while requested rows pass.
6. Fully proven filtered empty without exact requirements is `success_empty`.
7. Proven empty with an exact requirement fails as
   `required_contract_missing`.
8. Stale evidence remains rejected as `stale_data` by the existing freshness
   boundary.
9. Hash/schema/count contradictions are `internal_contract_error`.
10. Zero bid remains valid coverage and is left to strategy filtering.

## Completion evidence

- Focused required-data coverage, output-integrity, expected-contract,
  prefetch, snapshot, and account barrier tests pass.
- Relevant broader tick tests pass.
- Historical FUTU request/evidence shape is represented by a deterministic
  regression.
- Deepreview current-changes artifacts contain no unresolved findings.
- Final diff contains no change outside the files required by the slices above,
  except review artifacts and VERSION-driven release files in their later,
  separately authorized stages.
