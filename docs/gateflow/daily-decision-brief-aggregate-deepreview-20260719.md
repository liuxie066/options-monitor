# Aggregate Deepreview — Daily Decision Brief

- **Gate**: aggregate deepreview
- **Work unit**: `daily-decision-brief`
- **Selected base**: `5aecee73b3e4ace39b0c38ce9a98d18180020d1b` (`v1.2.420`)
- **Branch**: `codex/v1.3.0-daily-decision-brief`
- **Date**: 2026-07-19
- **Reviewer mode**: `deepreview --base 5aecee73`
- **Deepreview artifact**: `docs/reviews/code-review-20260719-192550.md`
- **Status**: findings accepted; fix required
- **Artifact path**: `docs/gateflow/daily-decision-brief-aggregate-deepreview-20260719.md`

## Findings

| Finding | Severity | Decision | Required fix |
|---|---|---|---|
| CR-AGG-1 immutable-revision crash permanently wedges allocation | High | accepted | Allocate after the maximum validated current/existing same-day revision; preserve orphan immutable files and add injected-crash regressions. |
| CR-AGG-2 high-priority action activation is silent | Medium | accepted | Emit a material existing `action_added` transition when a stable action enters active P0/P1, without duplicating P0 upgrade reporting. |

## Direct evidence

- Injecting a crash after `r0001.json` but before `current.json` leaves current at revision 0; every next prepare retries revision 1 and raises `daily brief revision already exists`.
- Same-ID P0 Close Advice transitions `blocked/observe/invalidated -> active` return `material=false` with an empty change list.

## Validation baseline

- Focused aggregate: `286 passed`.
- Full suite: `2793 passed, 10 skipped` after supplying the worktree-local ignored `.venv/bin/python` expected by legacy subprocess tests.
- Dependency graph: current, 475 production modules, zero cycles.
- Runtime config guard, release metadata, ruff, compileall and `git diff --check`: passed.

## Open Questions

无。

## Residual risks / uncovered areas

- Real provider idempotency remains production-canary evidence and is not exercised in this Draft PR work unit.
- Early-close calendars remain inherited scheduler scope and are assigned to a later work unit.
- No unclassified residual risk after accepting both findings into the fix loop.

## Gate decision

- **Decision**: fail pending fix.
- **Current gate**: aggregate deepreview fix.
- **Next entry point**: fix CR-AGG-1 and CR-AGG-2, add regressions, validate, then re-review.
