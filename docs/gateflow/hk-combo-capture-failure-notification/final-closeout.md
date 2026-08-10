# Gateflow Final Closeout — HK capture and failure notification

- Gate: `final closeout`
- Work unit: `hk-combo-capture-failure-notification`
- Branch: `fix/hk-combo-capture-failure-notification`
- Pull request: <https://github.com/liuxie066/options-monitor/pull/143>
- PR base at review: `main@47887347cac7f72be76d050ee2f8f8b532b0978e`
- Accepted PR review commit: `2ebb8972`
- Status: final closeout pass

## What changed

- Candidate capture statuses are normalized and partitioned by their owning
  snapshot before strict scope validation. Opening consumes only `put` / `call`;
  SP+LC and CC+LP consume their own `combo_yield` variant facts and pairs.
- Frozen required-data failure now emits the corresponding Combo capture fact,
  and CC+LP preserves `not_applicable` rather than collapsing it to a generic
  no-candidate result.
- A current-run portfolio identity receipt is published from validated prepared
  inputs before required-data prefetch and reused byte-for-byte by the complete
  Position Advice source graph.
- Barrier and pipeline failures can therefore reach the existing Daily Brief
  `fixed_failure` authority path. Invalid identity/config/receipt state remains an
  account-scoped no-send failure.

## What was verified

- Slice-level implementation and review gates passed; the S2 invalid-UTF-8 typed
  boundary finding was fixed and re-reviewed.
- Aggregate DeepReview passed with no remaining findings.
- PR-level DeepReview passed with no findings:
  `docs/reviews/pr-143-review-20260810-123511.md`.
- The five implementation/readiness commits replayed without conflict on
  `main@47887347`; detached integration head `d8380d02` passed 265 tests.
- Ruff, compileall, and Git diff checks passed.
- GitHub reported 5/5 checks passing on the reviewed implementation head before
  the docs-only PR review and closeout commits.

## Documentation

- Gateflow plan, review, implementation, readiness, PR review, and closeout
  artifacts are included in PR #143.
- No public CLI, schema, config, notification wording, or operator workflow was
  changed.
- The unrelated dirty `docs/DEPENDENCY_GRAPH.md` and all pre-existing
  service-credential/deploy/drift/secret-store changes remain outside this work
  unit and were never staged.

## Finding status

- Plan review: five accepted findings, all resolved in the accepted plan.
- S1 code review: no findings.
- S2 code review: one accepted finding, fixed and re-reviewed.
- Aggregate DeepReview: no findings.
- PR DeepReview: no findings.
- Blocking findings or open questions: none.

## Remaining risks and owners

| Residual risk | Owner / destination |
|---|---|
| Scheduler target retry after operational failure | Later scheduler-reliability work unit |
| OpenD expiration lookup timeout/retry | Later required-data/OpenD reliability work unit |
| Receipt freshness during unusually long runs | Existing 30-minute fail-closed validator plus operational evidence |
| Live delivery behavior | Separate release/upgrade authorization and natural-run provider-attempt/delivery-confirmed evidence |

No residual risk is unclassified.

## PR and issue status

- Draft PR: <https://github.com/liuxie066/options-monitor/pull/143>
- Issue link: not applicable; this work unit was initiated from the reported
  incident and no GitHub issue number was supplied.
- The user explicitly authorized commit, push, and merge to `main` on 2026-08-10.

## Next entry point

Push this closeout artifact, require the new PR head checks to pass, mark PR #143
ready for review, and merge it into `main`. Release, deployment, remote upgrade,
and production notification verification remain separate actions.
