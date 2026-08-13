# Gateflow Fix Artifact — S1 Code Review Fix

- Gate: `fix` after code review `docs/reviews/code-review-20260813-144738.md`
- Work unit: `candidate-filter-run-resolution`
- Slice: `S1`

## Finding decision and fix

### CR-1 — accepted — fixed

`iter_notification_perception_events` now returns `{events, total_count, truncated}` instead of a bare list. When the bounded scan window truncates before the requested `notification_date` can be ruled out, `candidate_filter_explain` raises `DEPENDENCY_MISSING` with `details.reason=audit_window_truncated` (plus `audit_total_count`) instead of the indistinguishable `no_notification_run`. Regression coverage: helper-level truncation assertion plus a monkeypatched impl-level test asserting the distinct reason.

Final status: `已修复`.

### CR-2 — pre-existing, recorded — deferred with owner

`candidate_filter_explain` base path bypasses `resolve_runtime_root` (pre-existing, shared by `latest` behavior before this slice). Deferred to a later work unit that moves candidate tools onto `resolve_runtime_root`; this slice kept audit read and snapshot read on the same base so no new inconsistency was introduced.

Final status: `deferred-with-owner` (later work unit: candidate tools runtime-root alignment).

## Validation after fix

- `./.venv/bin/python -m pytest tests/test_candidate_filter_run_resolution.py tests/test_candidate_filter_trace.py tests/test_candidate_snapshot_manifest.py tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py -q` -> 148 passed, 1 pre-existing deprecation warning

## Residual risks

- Unchanged from review: O(file) audit scan and in-memory read (accepted, later indexed read if hot).
