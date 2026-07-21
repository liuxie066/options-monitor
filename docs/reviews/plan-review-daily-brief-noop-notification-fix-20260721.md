# Plan Review — Daily Brief No-op Notification Fix

## Reviewed Target and Scope

- Gate: plan review
- Target: `docs/plans/daily-brief-noop-notification-fix-plan-20260721.md` revision 1
- Work unit: `daily-brief-noop-notification-fix`
- Status: changes required

## Executive Decision

**changes-required** — the proposed owning boundary is correct, but the planned eligibility predicate uses the wrong semantic authority and can still permit notifications that the account scheduler explicitly denied.

## Findings

### PR-01 — High — Delivery eligibility must follow `should_notify`, not a compound no-op heuristic

- Location: plan sections “State-machine refinement” and “Implementation Decision”.
- Planned behavior: skip only when `ran_scan=false && should_notify=false`.
- Direct evidence:
  - `AccountResult.should_notify` is the account-scoped notification decision passed into notification preparation.
  - The legacy notification path filters account output using `result.should_notify`.
  - `ran_scan` describes whether work ran; it is not authorization to send.
- Counterexample: `ran_scan=true, should_notify=false` can occur for a forced/smoke/closed-window or future scheduler path. Revision 1 would still assemble and deliver a Daily Brief despite the explicit notification denial.
- Required fix:
  - skip an account when `should_notify is False`;
  - use identity comparison so a missing field (`None`) is not silently reinterpreted as denial for compatibility with existing mapping fixtures/callers;
  - preserve `ran_scan=false, should_notify=true` and `ran_scan=true, should_notify=true` preparation, allowing intentional alerts and real pipeline-failure blocked briefs.
- Required tests:
  - no-op false/false is skipped end to end;
  - ran-scan true / should-notify false is also skipped;
  - pipeline failure with explicit should-notify true is still prepared as blocked;
  - mixed accounts prepare only accounts not explicitly denied.

Status: **accepted; plan revision required**.

## Assumptions / State-machine Review

- Real failed attempted pipelines currently expose `ran_scan=true` and retain the scheduler `should_notify` decision: supported by `tests/test_account_run.py` and `src/application/account_run.py`.
- Explicit `should_notify=false` is terminal for this tick's outbound account notification: supported by the account result contract and legacy preparation behavior.
- Missing `should_notify` should not be treated as false during this narrow bug fix because `TickNotificationRequest.results` accepts mappings without schema validation in focused tests and potentially internal callers.

## Special Review Lenses

- Architecture boundary: pass after required predicate fix; `tick_notification_flow` owns outbound preparation eligibility.
- State machine: changes required; `should_notify=false` must be an absorbing no-delivery decision for the tick.
- Semantic ownership drift: revision 1 incorrectly lets `ran_scan` participate in delivery authorization.
- Overcoupling: pass; no scheduler/repository/renderer changes are needed.
- Overengineering: pass; one explicit guard and tests are sufficient.
- Testing: changes required as listed in PR-01.

## Open Questions

None blocking after PR-01 is incorporated.

## Residual Risks

- Stale global `scheduled_target_market`: assigned to a separate diagnostics/state-consistency work unit.
- Historical remote revision 1: production cleanup remains separately approval-gated.
- Missing `should_notify` mappings remain eligible for compatibility: accepted internal-contract risk; production `AccountResult` always supplies the field.
