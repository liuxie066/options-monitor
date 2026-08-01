# Gateflow Plan — Shadow Replay integrity selection

- Gate: plan
- Work unit: `om-shadow-replay-integrity-selection`
- Base: `origin/main@8d467282`
- Design source: production receipt plus existing Shadow Replay integrity
  contract; no separate design document

## Goal, motivation, and success signal

The production Strategy Lab sampler selected the oldest five Shadow Replay
datasets even though their status reported `dataset_integrity.status` as
`legacy_unverified`. OpenD collection refreshed required-data and limiter/cache
state before the downstream dataset writer rejected each missing integrity
manifest. The run therefore spent provider capacity and then failed all five
actions without producing marks.

Success means write-mode planning excludes unverified datasets from the
`max_datasets` execution budget, direct collection validates integrity before
OpenD, legacy evidence remains untouched, and a production canary reaches
verified datasets without creating release-local runtime state.

## First-principles judgment and code evidence

Integrity eligibility is a precondition for mutation, not an action outcome.
The status owner already computes this fact in
`src/application/shadow_replay/status.py::_dataset_status_unlocked()`. The data
plan must carry that fact into its rows, and the executor must apply it before
the existing rate-limit circuit and `max_datasets` counter. Because status and
execution are separated in time, `collect_shadow_replay_marks()` must still
revalidate the dataset at the I/O boundary to fail closed under stale or direct
calls.

This preserves the existing owners: status derives the integrity fact, the
planner decides eligibility, and collection enforces the write precondition.

## Non-goals and scope boundary

- Do not synthesize, repair, backfill, delete, or overwrite historical manifests.
- Do not change dry-run planning, legacy read-only inspection, sorting priority,
  dataset schemas, integrity hashing, or provider limiter policy.
- Do not change notifications, runtime config, trade state, Feishu, broker, or
  trading behavior.
- Do not introduce a new eligibility framework or persistence layer.

## Affected files and ownership

- `src/application/shadow_replay/status.py`: project the existing compact
  integrity fact into data-plan rows.
- `src/application/shadow_replay/data_plan.py`: enforce write eligibility before
  rate-limit/max counters and expose explicit skip observability.
- `src/application/shadow_replay/collection.py`: revalidate at the direct
  write-mode I/O boundary before reading/fetching.
- `tests/test_shadow_replay.py`: planner quota and fail-before-fetch regressions.
- `tests/test_strategy_lab.py`: preserve the existing verified rate-limit path.
- `docs/STRATEGY_LAB_DESIGN.md`: document the verified-only write policy.
- `docs/DEPENDENCY_GRAPH.md`: regenerate after the new common-module import.

## Contract, state-machine, and public-interface decisions

- No schema-version or CLI signature changes.
- Existing `data_plan` rows gain `dataset_integrity`; action projections gain
  `dataset_integrity_status` and `dataset_integrity_reason`.
- Write-mode unverified actions transition to terminal local result
  `skipped/dataset_integrity_unverified`; they do not increment the action
  budget and do not open the OpenD rate-limit circuit.
- `summary.integrity_skipped_count` provides durable receipt observability.
- Dry-run remains descriptive and does not exclude legacy rows; only mutation
  eligibility changes.
- Direct `collect_shadow_replay_marks(write=True)` raises the existing integrity
  `ValueError` before OpenD or persistent cache activity. A verified status from
  planning is not accepted as write authority without revalidation.

## Implementation slice S1

- Objective: make dataset integrity a fail-closed precondition for write-mode
  planning and collection.
- Allowed files: only the affected files listed above and Gateflow/review
  artifacts for this work unit.
- Exact changes:
  1. Add a compact integrity projection to candidate and close data-plan rows.
  2. In `_run_plan_rows()`, after action enablement but before circuit/max logic,
     skip every write action whose projected status is not exactly `verified`.
  3. Preserve the skip status/reason in action and summary receipts.
  4. Revalidate at the start of write-mode collection before dataset reads and
     OpenD fetch setup.
  5. Update tests and design documentation.
- Error handling: malformed/mismatched manifests continue to fail during status
  construction; missing/old integrity receipts are observable skips in plan
  execution and hard failures for direct collection.
- Invariants: no legacy evidence mutation, no quota consumption for ineligible
  rows, no OpenD/cache side effect before direct validation, no dry-run writes.
- Stop condition: any required behavior needs manifest backfill, broader schema
  migration, or a new external write boundary.

## Validation

- Focused: `python3.12 -m pytest -q -p no:cacheprovider
  tests/test_shadow_replay.py tests/test_strategy_lab.py tests/test_research.py`.
  Expected: legacy skip does not consume limit; verified row executes; direct
  unverified collection never invokes fetch; existing rate-limit semantics pass.
- Static: Ruff on changed Python files, `git diff --check`, and
  `python3.12 scripts/generate_dependency_graph.py --check`.
- Full: `python3.12 -m pytest -q -p no:cacheprovider`; expected zero failures.
- Release: project release preflight, GitHub tag/Release SHA equality, remote
  `update verify`, `pip check`, service drift readback.
- Canary: controlled Strategy Lab sample must exit 0, record integrity skips,
  execute/defer only verified datasets, avoid a RATE_LIMIT storm, preserve the
  shared limiter policy, and create no release-local limiter/cache.

## Docs decision

Update Strategy Lab design because write-mode selection and observable receipt
semantics are operator-facing contracts. No user CLI examples need changing.

## Risks and open questions

- TOCTOU between status and execution is mitigated by direct collection
  revalidation. The underlying per-dataset lock remains the existing owner.
- A deployment with no verified datasets will exit successfully with only
  integrity skips; this is intentional because legacy evidence is ineligible,
  not an execution failure, and the skip count remains visible.
- Historical datasets remain unavailable to write-mode sampling until produced
  through a trusted manifest-bearing path; assigned to normal evidence
  regeneration, not this repair.
- No blocking open questions.

## Why this is not overdesigned

The change reuses the existing integrity result and validation function at the
two current decision boundaries. It adds no abstraction, storage, migration, or
compatibility shim, and avoids modifying unrelated provider/rate-limit logic.

## Completion report

Report the implementation, exact test/review results, merge/Release URLs and
SHAs, production version/readback, canary receipt outcome, write boundaries,
and any residual risk with an explicit owner.
