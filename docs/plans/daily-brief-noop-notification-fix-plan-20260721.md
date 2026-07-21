# Gateflow Plan — Daily Brief No-op Notification Fix

## Gate / Work Unit

- Gate: plan
- Work unit: `daily-brief-noop-notification-fix`
- Branch: `codex-fix-daily-brief-noop-notification`
- Base commit: `4576a1c106f6521b704877ed753ffe24683fa285`
- Date: 2026-07-21
- Status: accepted after plan review revision 2

## Goal / Motivation / Success Signal

### Goal

Prevent an account scheduler no-op (`ran_scan=false` and `should_notify=false`) from creating, diffing, persisting, or delivering a new Daily Decision Brief from an empty current run directory.

### Motivation

Remote HK evidence from 2026-07-21 showed:

- the 09:40 account scans completed and delivered revision 0;
- the 09:50 timer invocation correctly returned account results with `ran_scan=false`, `should_notify=false`, and “09:40 already processed”;
- `tick_notification_flow._prepare_daily_brief_notification()` nevertheless assembled revision 1 for both accounts;
- the assembler read the empty 09:50 run directories, classified all four decision sources as `source_artifact_missing`, changed the brief from `ready` to `blocked`, and delivered a false “数据异常 · 09:40 批次” delta.

### Success signals

1. An account result with explicit `should_notify=false` does not call `assemble_daily_decision_briefs()` or `prepare_daily_decision_brief()`.
2. A no-op-only tick (`ran_scan=false`, `should_notify=false`) produces no account messages and follows the existing `no_account_notification` finalization path.
3. A mixed tick skips only no-op accounts and still prepares eligible account briefs.
4. A genuine due-scan pipeline failure remains eligible for a blocked brief because its account result has `ran_scan=true`, `should_notify=true`, while `ran_pipeline_accounts` excludes it.
5. Existing full, delta, no-send, quiet-hour, multi-market, delivery-confirmation, and render-context tests continue to pass.

## Non-goals / Scope Boundary

- Do not change the systemd HK timer cadence.
- Do not change account-scoped scheduler state or global scheduler snapshot semantics.
- Do not merge `lx` and `sy` notifications; legitimate scheduled runs remain per-account.
- Do not change Daily Brief schemas, repository revision rules, delivery keys, renderer wording, or provider delivery behavior.
- Do not edit production config, runtime state, services, or remote deployment.
- Do not incorporate the unrelated uncommitted candidate-event-risk changes from the original worktree.

## First-principles Judgment and Direct Code Evidence

- The side-effect owner is `src/application/tick_notification_flow.py::_prepare_daily_brief_notification`: it decides which account results enter assembly, revision persistence, rendering, and delivery preparation.
- `src/application/account_run.py` returns scheduler no-ops as `AccountResult(ran_scan=false, should_notify=false, notification_text="")`.
- The same module returns a failed attempted pipeline with `ran_scan=true` in the tested pipeline failure contract, while `ran_pipeline_accounts` remains false for that account. This preserves true blocked alerts.
- `src/application/daily_decision_brief_service.py` correctly marks missing current-run artifacts as blockers when asked to assemble a failed/incomplete attempted run; changing that policy would hide real failures.
- Therefore the smallest owning-boundary fix is an eligibility guard before assembly, not renderer suppression or a repository workaround.

## Affected Files / Modules

### Allowed implementation files

- `src/application/tick_notification_flow.py`
- `tests/test_daily_decision_brief_notification_flow.py`

### Gateflow artifacts

- `docs/plans/daily-brief-noop-notification-fix-plan-20260721.md`
- review/implementation/deepreview/closeout artifacts under `docs/reviews/` and `docs/gateflow/`

No other runtime module is expected to change.

## Contract / Schema / State-machine / Public Interface Changes

- Public CLI/API: unchanged.
- Persisted schema: unchanged.
- Delivery key/idempotency contract: unchanged.
- State-machine refinement:
  - explicit notification denial (`should_notify=false`, regardless of `ran_scan`) -> no Daily Brief preparation -> existing no-account finalization when no other account is eligible;
  - attempted success (`should_notify=true`, pipeline succeeded) -> existing ready/degraded preparation;
  - attempted failure (`should_notify=true`, pipeline not in `ran_pipeline_accounts`) -> existing blocked preparation;
  - notify-only result (`ran_scan=false`, `should_notify=true`) -> preserve existing preparation behavior;
  - missing `should_notify` (`None`) -> preserve compatibility and existing preparation behavior rather than treating absence as an explicit denial.

## Implementation Decision

Inside `_prepare_daily_brief_notification()`:

1. Extract `account` and `should_notify` from either mapping or object account results.
2. If `should_notify is False`, skip that account before calling the assembler or repository. Use explicit identity comparison so a missing field remains backward-compatible instead of becoming an implicit denial.
3. Leave all downstream preparation, audit, rendering, diff, and delivery logic unchanged.
4. Do not add a new helper, DTO field, config key, or persisted skip artifact; existing account metrics and no-account notification audit already expose the reason.

This is deliberately minimal: one guard at the side-effect owner and focused regression tests.

## Implementation Slice

### Slice 1 — Gate no-op account briefs

- Objective: prevent false blocked revisions and duplicate notifications for already-processed account schedule points.
- Allowed runtime file: `src/application/tick_notification_flow.py`.
- Allowed test file: `tests/test_daily_decision_brief_notification_flow.py`.
- Exact changes:
  - normalize test fixtures so ordinary eligible results explicitly carry `ran_scan=true`, `should_notify=true`;
  - add a no-op-only flow test asserting assembler is not called, `daily_brief.prepared` is empty, no send occurs, and completion is `no_account_notification`;
  - add a scan-completed but `should_notify=false` test proving the notification decision is authoritative;
  - add a mixed-account preparation test asserting only accounts not explicitly denied are assembled;
  - add a real pipeline-failure preparation test asserting a blocked message is produced when `ran_scan=true`, `should_notify=true`, and the account is absent from `ran_pipeline_accounts`;
  - add the production eligibility guard.
- Expected outcome: the 09:40-success -> 09:50-no-op sequence cannot create revision 1 or send a false anomaly.
- Stop condition: evidence that a required user-visible alert intentionally carries `should_notify=false`, because that would contradict the existing notification-decision contract.

## Tests / Validation

### Focused

```bash
./.venv/bin/python -m pytest tests/test_daily_decision_brief_notification_flow.py tests/test_daily_decision_brief_service.py tests/test_account_run.py
```

### Broader notification/tick regression

```bash
./.venv/bin/python -m pytest \
  tests/test_daily_decision_brief_scenarios.py \
  tests/test_multi_account_tick.py \
  tests/test_multi_tick_notify_format.py \
  tests/test_unified_tick_entrypoint.py
```

### Static analysis

Use the repository-supported Ruff invocation on changed Python files if available; otherwise run `python -m compileall` on the changed module and report the unavailable tool explicitly.

## Docs Decision

No operator/user documentation change is required because public commands, payloads, config, message wording, and safety boundaries are unchanged. Gateflow artifacts document the bug and decision.

## Risks / Open Questions

### Classified risks

- `ran_scan=false, should_notify=true` and missing-`should_notify` semantics are preserved. Classification: preserved existing behavior and compatibility.
- The global scheduler snapshot may still display stale `scheduled_target_market`. Classification: separate diagnostics/state-consistency work unit; no user-visible duplicate remains once no-op delivery is suppressed.
- Existing historical false revision remains in remote runtime state. Classification: production cleanup is out of scope and requires separate approval.
- Legitimate per-account `lx`/`sy` messages remain two deliveries. Classification: explicitly accepted product behavior.

### Blocking open questions

None after user confirmation of isolated worktree and per-account delivery preservation.

## Completion Report Format

- changed files and behavior;
- focused/broader validation commands and results;
- review/deepreview finding status;
- docs decision;
- residual risks and owners;
- commits, branch, and draft PR status if Gateflow reaches that gate.
