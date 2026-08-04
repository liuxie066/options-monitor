# Gateflow S1 Trading-Date and Architecture Scope Amendment

- Gate: plan amendment before S1 implementation acceptance
- Work unit: `required-data-multi-account-integrity`
- Slice: S1 — Required-data completion, receipt, seal, and gateway truth
- Artifact path: `docs/gateflow/required-data-multi-account-integrity/s1-trading-date-architecture-scope-amendment.md`
- Status: accepted (`pass-with-risks`)
- PlanReview artifact: `docs/reviews/plan-review-20260804-150918.md`

## Evidence

The S1 expected contract now makes expiration-discovery `trading_date` the
single authority for request projection, row DTE, raw provider evidence, and
ordered child evidence. The actual expiration discovery call is owned by
`src/application/opend_symbol_chain_fetching.py`. Without passing the frozen
date through that owner, the provider path can observe a different calendar day
from the contract even though downstream validators are strict.

The existing single-spec subprocess adapter is owned by
`src/application/opend_symbol_fetching_cli.py`. It must accept the same frozen
date and pass it to the existing fetch surface; otherwise in-process and
subprocess S1 paths use different clocks. This is only the single-spec date
anchor. S6 still owns multi-spec JSON plumbing and exact-spec subprocess
execution.

The architecture re-review also found that the snapshot owner imported the
Close Advice plan owner only to validate an optional attachment. This created a
production module cycle. Generic snapshot loading now validates generic
snapshot identity and bytes; seal-time and actual Close Advice consumers retain
the owner-specific attachment validation. The generated dependency graph must
be refreshed so the repository's checked architecture artifact proves the cycle
is absent.

## Exact scope addition

- Add `src/application/opend_symbol_chain_fetching.py` to S1 solely to thread
  the frozen trading date into expiration discovery/cache identity.
- Add `src/application/opend_symbol_fetching_cli.py` and its directly
  corresponding tests solely to parse and forward `--trading-date` for the
  existing single-spec subprocess path.
- Add generated `docs/DEPENDENCY_GRAPH.md` and `docs/dependency_graph.mmd` after
  running the repository dependency-graph generator from the final S1 source.
- Keep `required_data_snapshot.py` independent of the Close Advice owner. The
  generic loader validates only snapshot schema/content hash, run/root, and
  quote evidence. Seal and terminal reseal validate only the attachment's safe
  relative path, exact file bytes, and declared SHA-256, reporting
  `RequiredDataSnapshotError`. The Close Advice resolver exclusively validates
  the attached plan schema, run/status/content hash, and business structure,
  reporting `CloseAdviceRequiredDataPlanError`; the runner converts that failure
  to `snapshot_integrity_failed` and cannot promote output or notification.
- Do not add a new public `./om` command, config key, provider call, cache,
  schema, multi-spec CLI payload, Close Advice behavior, or S2-S7 owner.

## Success signal

- Expiration discovery, every provider child, aggregate raw metadata, and row
  DTE bind to one strict ISO trading date.
- The existing single-spec subprocess invocation forwards that exact date; a
  missing or different raw/child date cannot receive a quote receipt.
- Generic snapshot loading has no production import cycle with Close Advice,
  while seal and actual consumers still reject an invalid optional attachment.
- `scripts/generate_dependency_graph.py --check` reports a current graph with
  zero production module cycles.
- Shadow Replay remains byte-for-byte unchanged in S1.

## Validation

- Chain and CLI tests prove the supplied trading date reaches expiration
  discovery and the provider fetch call.
- Output, receipt, prefetch, and snapshot tests reject missing, inconsistent,
  or forged aggregate/child trading dates with zero receipt authority.
- Close Advice tests retain the typed attachment validation at the actual
  consumer boundary.
- Dependency-graph generator test and raw-fetch architecture guard pass after
  regeneration.
- The focused S1 suite, Ruff, compileall, `git diff --check`, and a timestamped
  DeepReview must pass before the protected local S1 commit.

## Residual risk

- The internal provider CLI still represents only one exact spec. S6 owns its
  multi-spec JSON contract and removes S1's typed unsupported boundary.
- `RequiredDataFetchPlanBundle.require_realized_volatility=False` remains a
  compatibility default. Canonical scheduled constructors explicitly set the
  authority; removing the default requires a later coordinated shadow/offline
  migration.
- Shadow Replay's existing union bundle still combines child RV demand with a
  false plan-level default and lacks a frozen trading date. It is offline and
  unchanged by S1; strict scheduled publication must not accept that bundle.
