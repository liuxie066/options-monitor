# Gateflow Fix Artifact — Slice A

- **Work unit**: `candidate-event-risk-monitoring`
- **Slice**: A
- **Gate**: code review fix
- **Finding**: `SA-01`
- **Review**: `docs/reviews/code-review-20260721-093657.md`
- **Artifact path**: `docs/gateflow/candidate-event-risk-monitoring-slice-a-fix-20260721.md`

## Fix

`EventStore._normalize_fetch_payload()` now validates structured evidence before accepting provider success:

- `events` must be a list;
- every event item must be an object/mapping;
- invalid structure raises into the existing source-error/cooldown path;
- malformed coverage can no longer be paired with a silently fabricated empty event list.

Legacy list fetchers remain supported with unknown coverage.

## Validation

A new regression proves malformed structured evidence produces `source_status=error`, empty snapshot coverage, and no successful event/coverage cache entry.

## Finding Status

- `SA-01`: **已修复**.

## Residual Risks

- Semantically malformed type/date fields inside otherwise object-shaped rows are validated fail-closed in approved Slice B candidate projection.
- Live provider drift remains assigned to later authorized canary.

## Completion Status

Fix complete; ready for Slice A re-review.
