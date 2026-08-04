# Gateflow Implementation — S1 Required-Data Completion Authority

## Gate

- Work unit: `required-data-multi-account-integrity`
- Slice: `S1`
- Gate: `implementation`
- Status: accepted; final aggregate DeepReview found zero material issues;
  pending protected local S1 commit
- Accepted/frozen base: `ed2531e956f7ff818b3198a6dc3d918bb5328b3f`
  (`origin/main` at plan acceptance). During final review, `origin/main` advanced
  independently by 11 commits to `6bc86f81361882b4175eb456a0ae31e36af7bc6c`;
  S1 remains on the accepted lineage and has not been rebased or merged.
- Branch: `fix/required-data-multi-account-integrity`
- Artifact path: `docs/gateflow/required-data-multi-account-integrity/s1-implementation.md`
- Approved scope amendments:
  - `docs/gateflow/required-data-multi-account-integrity/s1-scope-amendment.md`
  - `docs/gateflow/required-data-multi-account-integrity/s1-receipt-commit-scope-amendment.md`
  - `docs/gateflow/required-data-multi-account-integrity/s1-exact-position-scope-amendment.md`
  - `docs/gateflow/required-data-multi-account-integrity/s1-trading-date-architecture-scope-amendment.md`

## Scope and changed files

S1 changes only the approved required-data provider, finalization, receipt, seal,
gateway-health boundaries, their plan artifact, and directly corresponding tests.

Production files:

- `src/application/opend_market_snapshot_fetching.py`
- `src/application/opend_symbol_chain_fetching.py`
- `src/application/opend_symbol_fetching.py`
- `src/application/opend_symbol_fetching_cli.py`
- `src/application/opend_symbol_outputs.py`
- `src/application/position_advice_source_receipts.py`
- `src/application/required_data_fetching.py`
- `src/application/required_data_coverage.py`
- `src/application/required_data_plan_identity.py`
- `src/application/required_data_planning.py`
- `src/application/required_data_snapshot.py`
- `src/application/required_data_steps.py`
- `src/application/multi_tick/required_data_prefetch.py`
- `src/infrastructure/futu_gateway_pool.py`

Generated architecture artifacts:

- `docs/DEPENDENCY_GRAPH.md`
- `docs/dependency_graph.mmd`

Direct test and Gateflow evidence files are the changed files reported by
`git status --short`, including the new strict snapshot/provider tests, receipt
tests, consumer-boundary tests, gateway-pool tests, and the four accepted scope
amendments and PlanReview artifacts.

## Implementation decisions and outcomes

1. Market snapshot success now means exact requested-code coverage. Provider
   results expose requested, returned, missing, and unexpected code sets and
   counts. Unexpected rows are discarded; a strict subset remains a typed
   incomplete result after fallback, while full fallback recovery may succeed.
   Duplicate provider rows fail in the original batch, across batches, and in
   fallback; the conflicting code is removed instead of using last-write-wins.
2. A required RV observation is a publication postcondition. Provider errors,
   missing or non-positive/non-finite RV, incomplete snapshots, wrong bindings,
   contradictory outcomes, invalid timestamps, and persistence row loss cannot
   acquire receipt authority. RV meta and every output row exact-bind the
   explicit 20/60/120/estimate fields; boolean, non-finite, negative, missing,
   or contradictory evidence fails. Valid no-contract discovery carries explicit
   `not_applicable_no_contracts` RV evidence and a canonical header-only CSV.
3. The expected fetch contract is built before execution and binds the exact
   symbol, physical source/host/integer-port, strict discovery identity, explicit fetch
   requests, coverage policy, and canonical SHA-256. Malformed success plans,
   including `success_rows` without declared requests or expiration targets,
   fail before gateway construction or receipt publication.
4. Fresh, cached, subprocess, and success-empty publication paths converge on
   one finalizer. It validates raw evidence, reads back the exact JSON/CSV,
   applies the existing multiplier normalization, revalidates coverage, and
   only then publishes or adopts an exact immutable receipt. Cached mode never
   calls `save_outputs`.
5. Manual/non-run fallback may persist a fully validated candidate for local
   compatibility, but it cannot synthesize a fetch plan, publish a receipt, or
   adopt old run authority. A planned path without `producer_run_id` also cannot
   weakly adopt a prior receipt.
6. Receipt publication remains payload-first and receipt-last. Its optional
   generic commit validator runs after receipt construction/validation and
   immediately before the immutable receipt write. Required-data uses a newly
   sampled production wall clock to revalidate aggregate and every child
   observation; expiry leaves at most an unreferenced orphan payload and zero
   completion receipt.
7. Same-run crash recovery adopts one exact contract/observation/byte match.
   Any other committed same-run receipt is a typed conflict. The terminal seal
   exact-compares global-plan and receipt contracts, records each symbol as
   ready or typed failed, and emits complete/partial/failed aggregate status.
8. Gateway health is changed only after typed provider-result validation.
   Cleanup closes the thread-local gateway and resets local failure count
   directly; cleanup does not masquerade as a successful provider observation.
9. Every ready position requirement retains its exact side, expiration, strike,
   and discovery-day DTE. Same-side strategy/position union preserves one-sided
   unbounded strike ranges and expands DTE ranges to include exact positions.
   Typed and debug coverage share the same absolute `1e-9` exact-strike rule.
10. In-process multi-spec execution invokes every ordered child on one gateway,
    binds the actual child identity to its planned request SHA, and performs one
    aggregate save/finalize. Child failures retain their original structured
    payload for gateway health. Duplicate contracts fail within a child;
    cross-child overlap is reconciled only when the complete canonical rows are
    identical and is recorded explicitly. When RV is required, every child must
    carry successful canonical 20/60/120/estimate evidence matching the aggregate
    and rows; missing, failed, or drifting child RV cannot reach persistence or a
    receipt. Subprocess multi-spec remains typed unsupported before execution or
    publication until S6 adds exact-spec CLI plumbing.
11. Expiration DTE has one semantic owner. The expiration-discovery trading
    date is the sole date authority; expected-contract validation and both
    typed/debug coverage recompute calendar-day DTE for every explicit
    expiration. Chain discovery, aggregate raw metadata, and each ordered child
    must bind that exact date. Forged ranges, missing/different dates, and
    missing, boolean, non-finite, fractional, or mismatched CSV DTE values fail
    closed, while empty top-level side-plan/projection evidence retains its
    no-row semantics and RV demand without creating an executable child.
12. Timestamp evidence must be timezone-aware; naive provider observations or
    completions cannot be interpreted as UTC. Raw, operational policy, receipt,
    and manifest ports are non-boolean integers and cannot be silently
    truncated from floats. Generic snapshot loading no longer imports the Close
    Advice plan owner: seal/reseal binds optional attachment bytes and hash,
    while the Close Advice resolver owns plan semantics. The regenerated
    dependency graph reports zero production module cycles.

## Validation

- Final changed-file S1 regression set: `508 passed`.
- Final child-RV/provider/finalizer subset: `128 passed`.
- Independent post-fix provider re-review: zero material residual; child-RV
  negatives/positive `9 passed` and real multi-spec merge/finalize `1 passed`.
- Earlier strict cross-contract subset: `278 passed`; an independent contract
  audit also passed `274` tests before the final provider audit exposed the
  child-RV reconciliation gap. That finding is now fixed and is covered by the
  final changed-file and provider/finalizer sets above.
- Broad non-environment suite after the child-RV fix: `4199 passed, 10 skipped`;
  four CLI-only test
  files were excluded because this isolated worktree intentionally has no
  `.venv/bin/python`. An unfiltered run confirmed the corresponding 18 failures
  were all that missing executable, not application assertions.
- `python3 -m compileall -q` over all changed S1 production owners: pass.
- Ruff over all S1 production and directly corresponding changed tests: pass.
- `git diff --check`: pass.
- Dependency graph regeneration/check: `577` production modules, `0` cycles;
  architecture tests `3 passed`.
- Production-clock TTL regression proves three independent samples: initial
  validation and pre-publication validation at 29 minutes, then receipt-commit
  validation at 31 minutes; result is one orphan payload and zero receipt.
- No live OpenD call, notification, broker/ledger write, authored/generated
  config mutation, production service action, release, deployment, merge, push,
  or external data write was performed.

## Docs decision

The public `./om` facade, configuration keys, output paths, receipt schema, and
operator workflow are unchanged. The internal provider CLI now accepts
`--trading-date` solely to preserve the frozen single-spec subprocess date; S6
still owns exact multi-spec CLI plumbing. The checked dependency graph artifacts
were regenerated after the import-cycle fix. The plan, scope amendments,
PlanReview evidence, and this implementation artifact record those changes.

## Residual risks and uncovered areas

- Scoped-union correction, run-local spot tri-state reuse, and subprocess/public
  CLI exact-spec plumbing are `covered by later approved slice` S6. S1 already
  executes exact ordered in-process children and temporarily rejects
  subprocess multi-spec before effects.
- Candidate CSV verification followed by reopening a mutable path is an
  explicitly accepted non-goal, `assigned to later work unit`; current
  run-scoped producer/seal semantics and Close Advice exact-byte path are
  unchanged.
- Broad manual multiplier/timestamp harmonization and future multi-binding
  physical filename redesign are explicit non-goals, `assigned to later work
  unit`.
- A commit-time validator rejection can leave an immutable orphan payload. This
  is `accepted with rationale`: the payload is not discoverable as completed
  authority without its receipt, write-once re-entry can verify the same bytes,
  and deleting it would weaken crash safety.
- Cross-process/distributed publication coordination is `accepted with
  rationale`: S1 preserves write-once exact-byte conflict detection and does not
  introduce the out-of-scope distributed lock or new persistence layer.
- The offline Shadow Replay combo bundle still relies on the plan-level RV
  default and an ambient trading date while its child specs request RV. It is
  unchanged in S1 and cannot pass the strict scheduled contract; coordinating
  that offline schema migration is `assigned to later work unit`.

## Completion status

All accepted S1 findings and the independent follow-up findings are fixed, and
the implementation validations pass. Final aggregate DeepReview
`docs/reviews/code-review-20260804-153946.md` found zero material issues. The
next Gateflow entry point is the protected local S1 commit; no integration,
push, release, deployment, or production action is included.
