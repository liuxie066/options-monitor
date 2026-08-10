# Gateflow Draft PR Readiness — HK capture and failure notification

- Gate: `ready-to-open-draft-PR`
- Work unit: `hk-combo-capture-failure-notification`
- Branch: `fix/hk-combo-capture-failure-notification`
- Original base: `main@0d635e11` (`v1.13.0`)
- Current PR base checked: `origin/main@f8583513` (`v1.13.2`)
- Accepted plan: `6e0d8964`
- Accepted S1: `7241fe9a`
- Accepted S2: `aef308a0`
- Accepted aggregate review: `af461f3b`
- Status: ready to push and open Draft PR

## Main-drift integration boundary

`origin/main` advanced during this work unit. The current-main path set and this
branch's committed path set do not overlap, and a three-way `git merge-tree`
preview contains no conflict markers.

To avoid touching the user's dirty worktree, the four accepted work-unit commits
were replayed only in a detached `/tmp` worktree on `origin/main@f8583513`. The
resulting integration head was `04a09a6d`; no branch or production/runtime state
was changed by that validation. The PR may therefore retain its original commit
lineage while GitHub forms the already-validated merge result against current
main.

## Intended committed diff

- owner-aware opening/SP+LC/CC+LP capture routing and status reduction;
- current-run portfolio receipt publish/validate/exact-byte reuse before the
  required-data barrier and during recovery;
- account-scoped typed conflict handling and fixed-failure liveness through the
  existing Daily Brief authority;
- focused/integration tests and Gateflow/planreview/deepreview artifacts.

No VERSION, CHANGELOG, public docs, public schema/config/CLI/notification wording,
scheduler retry, OpenD timeout policy, runtime, service or deployment file belongs
to the work-unit diff.

## Validation

```text
Original branch aggregate: 265 passed, 4 warnings
Latest-main detached integration aggregate: 265 passed, 4 warnings
Ruff on all changed Python production/tests: pass on both heads
compileall domain src scripts: pass on both heads
git diff --check: pass on both heads
Three-way merge preview against origin/main@f8583513: no conflicts
```

The warnings are existing legacy renderer deprecations. Validation used temporary
runtime, fake providers or no-send paths and made no live external calls.

## Dirty-worktree boundary

The original README/operator/security/service credential/deploy/drift/secret-store
and service test changes remain unstaged and uncommitted. The readiness commit
must stage only this artifact. `docs/DEPENDENCY_GRAPH.md` remains untouched by the
work unit.

## Residual risks and owners

- Scheduler target retry after failure: later scheduler reliability work unit.
- OpenD expiration timeout/retry: later required-data/OpenD reliability work unit.
- Long-run portfolio receipt freshness: existing 30-minute fail-closed validator.
- Live behavior: separate release and remote-upgrade authorization after human PR
  review and merge.

All approved slices and aggregate review gates are complete. Next transition:
push the branch, open a Draft PR, then perform PR-level DeepReview. Gateflow does
not authorize merge, release, deployment or production notification replay.
