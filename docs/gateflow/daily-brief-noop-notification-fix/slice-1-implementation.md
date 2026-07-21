# Implementation Artifact — Daily Brief No-op Notification Fix Slice 1

## Gate / Scope

- Gate: implementation
- Work unit: `daily-brief-noop-notification-fix`
- Slice: 1 — Gate explicitly denied account briefs
- Base: accepted plan commit `85375d79`
- Status: implementation complete; awaiting code review

## Changed Files

- `src/application/tick_notification_flow.py`
- `tests/test_daily_decision_brief_notification_flow.py`

## Implementation

`_prepare_daily_brief_notification()` now reads the account result's `should_notify` field from either a mapping or object and immediately skips the account when the value is explicitly `False`.

The guard executes before:

- Daily Brief assembly from current-run artifacts;
- repository revision/diff persistence;
- rendering;
- delivery-key creation;
- provider routing and send preparation.

Missing `should_notify` remains eligible for compatibility. Explicit `True` remains eligible, including genuine pipeline failures excluded from `ran_pipeline_accounts`, which continue through blocked-brief assembly.

## Regression Coverage Added

- no-op account does not assemble, resolve delivery, persist a prepared lifecycle, or send;
- completed scan with explicit notification denial is skipped;
- mixed accounts prepare only the eligible account;
- pipeline failure with notification allowed still produces a blocked “数据异常” brief.

## Validation

- Red test before implementation: 3 new tests failed because denied accounts were assembled.
- Focused notification flow after implementation: `15 passed`.
- Focused service/account suite: `53 passed`.
- Broader notification/tick suite: `46 passed`.
- Ruff on changed Python files: pass.
- `git diff --check`: pass.

## Docs Decision

No public/operator docs changed because command, configuration, payload, schema, renderer text, and deployment behavior are unchanged. Plan/review/implementation artifacts document the behavior.

## Residual Risks

- Production account results are objects while the newly added explicit-denial tests currently use mappings. Classification: code review must verify or request a production-shape test.
- Stale global scheduler target remains visible in diagnostics. Classification: separate work unit.
- Historical remote false revision remains. Classification: separate production-approval-gated cleanup.
- Per-account `lx`/`sy` delivery remains intentional. Classification: accepted product behavior.

## Code Review Outcome

- Review: `docs/reviews/code-review-20260721-101032.md`
- Accepted finding `CR-01`: 已修复
- Re-review: `docs/reviews/code-rereview-20260721-101153.md`
- Final slice status: accepted for checkpoint commit
