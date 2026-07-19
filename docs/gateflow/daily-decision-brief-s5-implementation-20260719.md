# Gateflow Implementation — Daily Decision Brief S5

- **Gate**: implementation
- **Work unit**: `daily-decision-brief`
- **Slice**: S5 — scenario/regression closure
- **Date**: 2026-07-19
- **Base**: accepted S4 commit `c2401643`
- **Status**: implementation complete; ready for code review
- **Artifact path**: `docs/gateflow/daily-decision-brief-s5-implementation-20260719.md`

## Objective and completion signal

Close the approved cross-module scenario matrix without adding product behavior. The new tests exercise real scheduler, domain diff, repository delivery-pointer, read-view and notification-flow entry points so the Daily Brief baseline/delta relationship is proven across boundaries rather than only in isolated unit tests.

Completion signal: all 11 approved scenarios are represented and pass; existing Daily Brief, scheduler and Agent contracts remain green.

## Changed files

- `tests/test_daily_decision_brief_scenarios.py`
- `docs/gateflow/daily-decision-brief-s5-implementation-20260719.md`

No production module, config, generated artifact, VERSION or CHANGELOG changed.

## Scenario coverage

1. **09:40 first full for lx/sy**
   - Real `scan_scheduler.decide()` returns due/notify-open for both accounts at 09:40 US market time.
   - Real repository allocates independent revision 0 full lifecycles for `lx` and `sy`.

2. **Same-day unchanged silent**
   - After confirming the full pointer, a new revision with only price/metric noise is non-material and `delivery_kind=none`.

3. **New/upgrade P0**
   - Domain diff emits material `p0_added` and `priority_upgraded_to_p0` changes.

4. **Main action invalidated**
   - Same stable action identity changing active -> invalidated emits material `action_invalidated`.

5. **Blocked -> recovery**
   - Actionability transition emits material `recovered`.

6. **Whole-contract capacity vs cash noise**
   - Cash-only change with the same whole-contract capacity is silent.
   - `contracts_available` 1 -> 2 emits material `capacity_changed`.

7. **Failed delta retained against last delivered**
   - An unconfirmed revision 1 does not move the pointer.
   - Revision 2 still diffs from confirmed revision 0 and retains the P0 upgrade delta.

8. **Post-close planning-only**
   - Stored brief remains `live_actionable` for audit; read-time effective actionability and Markdown become planning-only after `valid_until_utc`.

9. **All-day no-run does not create fake LIVE**
   - A schedule with all run points suppressed by a full-session break never opens scan/notify.
   - Read surface remains unavailable and no account state directory is created.

10. **Old runtime unavailable**
    - Exact historical revision read without an artifact returns structured `not_found`, masked expected state path and unavailable Markdown.

11. **Disabled exact legacy behavior**
    - `notifications.daily_brief.enabled=false` never calls Daily Brief preparation.
    - Existing `prepare_multi_account_notification` path is called exactly once and legacy no-account completion semantics remain unchanged.

## Validation

```text
python3 -m pytest -q tests/test_daily_decision_brief_scenarios.py
11 passed

python3 -m pytest -q \
  tests/test_daily_decision_brief_*.py \
  tests/test_scan_scheduler_notify_semantics.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py
176 passed

python3 -m ruff check tests/test_daily_decision_brief_scenarios.py
All checks passed

python3 -m compileall -q tests/test_daily_decision_brief_scenarios.py
passed

git diff --check
passed
```

## Docs decision

No public behavior changes; S4 public docs remain current. This slice adds only durable scenario proof and its Gateflow artifacts.

## Residual risks / uncovered areas

- Real market-holiday selection is owned by the existing market-selection/runtime boundary; this scenario verifies the invariant once all run points are suppressed, not an exchange calendar implementation.
- Real provider behavior and production notification noise remain assigned to separately authorized canary/observation.
- Historical migration remains a later work unit; explicit unavailable is the accepted behavior.
- No unclassified residual risk.

## Gate transition

- **Current gate**: S5 code review.
- **Next entry point**: run `deepreview --base c2401643`, adjudicate findings, fix/re-review, then create the accepted S5 slice commit only after pass.
