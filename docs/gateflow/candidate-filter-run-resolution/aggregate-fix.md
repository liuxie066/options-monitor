# Gateflow Fix Artifact — Aggregate DeepReview Fix

- Gate: `fix` after aggregate deepreview `docs/reviews/code-review-20260813-150303.md`
- Work unit: `candidate-filter-run-resolution`

## Finding decision and fix

### DR-1 — accepted — fixed

`latest_notification` 分支解析出的 run 快照加载失败时，`DEPENDENCY_MISSING` 错误 details 现在携带 `reason=snapshot_unavailable_for_notification_run`、已解析的 `run_id` 和 `notification_date`，不再回退为 `run_id=None`。新增回归测试 `test_notification_run_with_missing_snapshot_reports_resolved_run_id`。

Final status: `已修复`.

## Validation after fix

- 149 passed（focused 16 + trace/manifest/contract/smoke 回归），1 pre-existing deprecation warning。

## Residual risks

- CR-2 runtime-root alignment：deferred-with-owner（later work unit）。
- O(file) audit scan：accepted，later indexed read if hot。
