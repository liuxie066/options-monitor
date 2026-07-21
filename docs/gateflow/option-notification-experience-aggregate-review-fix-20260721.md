# Gateflow Aggregate Deepreview Fix — Option Notification Experience

- Gate: aggregate deepreview fix
- Work unit: option-notification-experience
- Date: 2026-07-21
- Status: fixed, pending re-review
- Source review: `docs/reviews/code-review-20260721-203532.md`

## Accepted findings and fixes

### AG-001 — Daily Brief disabled skipped scheduler watermark

Moved the existing target-commit step out of the Daily Brief-only branch. Exact scan targets are now committed after either Daily Brief or legacy notification preparation succeeds and before any provider send. Scheduler progress no longer depends on the projection feature flag.

### AG-002 — manual/force scans had ordinary delivery side effects

Non-scheduled scans now:

- persist reliable successful current snapshots;
- skip candidate delivery-state recording;
- choose no fixed/candidate delivery action;
- do not select or send an existing ordinary scheduled envelope;
- update only the scheduler completion observation through `account -> None`, without advancing processed-target state.

### AG-003 — per-account legacy seed was lost after another account migrated

`_last_processed_scan_target_for_account` now falls back to that account's legacy `last_run` whenever the new processed-target map lacks that account. Once the account has an explicit new entry, that entry remains authoritative.

### AG-004 — scheduled fallback could send without exact target commit

Added a fail-closed precondition at the notification-flow entry: every account result that reports `ran_scan=True` under a scheduled trigger must have a non-empty exact account target. Missing mappings are audited as `SCHEDULED_SCAN_TARGET_MISSING` and raise before snapshot/envelope preparation, target commit, or provider send.

## Changed files

- `src/application/scan_scheduler.py`
- `src/application/tick_account_execution.py`
- `src/application/multi_account_tick.py`
- `src/application/tick_notification_flow.py`
- `tests/test_scan_scheduler_notify_semantics.py`
- `tests/test_multi_account_tick.py`
- `tests/test_daily_decision_brief_notification_flow.py`
- `docs/DEPENDENCY_GRAPH.md`

## Validation

```text
Aggregate focused scheduler/notification tests: 44 passed
Daily Brief focused suite: 136 passed
Scheduler + multi-tick focused suite: 100 passed
Agent/plugin/dependency + critical flow suite: 149 passed
Full repository: 2947 passed, 10 skipped
ruff: pass
compileall: pass
dependency graph: current, 477 production modules, 0 cycles
git diff --check: pass
US example config validation: ok=true
HK example config validation: ok=true
```

## Docs decision

No public documentation change is required. These fixes restore contracts already stated in the approved implementation plan and user docs; adding a new public concept would be misleading.

## Residual risks

- Production capacity, live-provider behavior, confirmed pointer migration, release, remote upgrade, and next-normal-target observation remain separate approval-gated rollout work.
- The first upgraded target still uses legacy completion time as a conservative per-account seed until that account writes its first exact processed target; this remains an accepted rollout observation risk.
- No unclassified aggregate-review risk remains.
