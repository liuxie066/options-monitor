# Goal Confirmation — HK Daily Decision Brief Canary Correction

- **Gate**: goal confirmation
- **Work unit**: `daily-decision-brief-canary-correction`
- **Date**: 2026-07-20
- **Status**: confirmed by user
- **Base**: `main` / `2e850529` / `v1.3.1`
- **Branch**: `fix/daily-brief-canary-correction`
- **Artifact path**: `docs/gateflow/daily-decision-brief-canary-fix-goal-confirmation-20260720.md`

## Goal

Correct the post-v1.3.0 HK no-send Canary defects while preserving the existing Daily Brief lifecycle:

1. Sell Put Daily Brief consumes only canonical labeled artifacts and never falls back to raw rows.
2. CLI and Agent Tool read the canonical stateful runtime root.
3. Payload and Markdown keep active actions, candidate evidence, data quality, and gaps unambiguous.
4. Production no-send evidence proves the persisted brief, prepared notification, CLI, and Agent Tool refer to the same immutable revision.
5. Release and remote upgrade follow the normal VERSION-driven path; real sending remains separately authorized.

## Motivation and direct evidence

- Current assembler loads both `*_sell_put_candidates_labeled.csv` and `*_sell_put_candidates.csv`, then dedupes/reranks them.
- HK production Canary showed accepted labeled P430/P440 rows but the Daily Brief selected raw-only P450 rows without capacity.
- CLI and Agent Tool pass repo root directly instead of using the existing runtime-root resolver, so release-local shadow state can be read instead of production state.
- Existing notification, repository, revision, diff, delivery-pointer, and no-send behavior otherwise remained safe and must not be redesigned.

## Success signals

- Artifact empty/missing/malformed/partial semantics match the accepted plan and fixtures.
- Raw-only rows cannot enter candidates, actions, events, summary, or rendered Markdown.
- `OM_RUNTIME_ROOT` wins in real CLI/Agent read integration tests.
- Only `actions[state=active]` are executable; renderer exposes both actionability and data quality.
- Focused, subsystem, Agent contract, release, and production no-send validation pass.
- Delivery pointer remains unchanged and provider send count remains zero during Canary.

## Non-goals

- No strategy threshold or ranking-policy change.
- No schema v2, DB migration, queue, scheduler, or second delivery stack.
- No global `repo_base()` change.
- No repository/diff/delivery-key state-machine change.
- No event rendering fix in this work unit.
- No real notification send without a separate explicit user authorization.

## Scope boundary

Implementation follows:

- `docs/gateflow/daily-decision-brief-canary-fix-plan-20260720.md`
- final accepted plan review: `docs/reviews/plan-review-20260720-114038.md`

The user confirmed creation of the work branch and execution on 2026-07-20.

## Blocking open questions

None. The accepted plan resolves canonical empty encoding, render-context replay, exact-revision Canary reads, and event deferral.

## Gate transition

- **Current gate**: goal confirmation pass
- **Next gate**: accepted plan commit, then implementation S1
