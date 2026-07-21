# Gateflow Merge Fix — Option Notification Experience vs latest main

## Gate

- Work unit: `option-notification-experience`
- Gate: post-PR main-merge integration / re-review
- Feature parent: `a318503713923fa9e74aac4bfa228d602ab6e167`
- Merged base: `origin/main` at `99c12f570d80d81b6bf87b144e5cc92e62da438f`
- Status: pass after fix/re-review; ready to conclude the merge commit

## Conflict Cause

PR #109 and latest `main` both changed Daily Brief rendering and its contract tests:

- PR #109 introduced fixed-report, candidate-alert, failure, and query projections with funds/reminder semantics.
- `main` introduced channel-aware mobile-flat Markdown for notification surfaces.
- Git conflicts occurred in the Daily Brief renderer and its renderer/notification-flow tests; documentation examples also required semantic reconciliation.

## Resolution Decisions

1. Keep one canonical strategy scan and one notification authority path.
2. Keep fixed report precedence: when fixed-report and new-candidate conditions both hold, send one complete fixed report.
3. Fixed reports render `状态｜<HH:MM> 批次` rather than adding a redundant `固定报告` label.
4. Candidate alerts remain candidate-specific, suppress positions, and retain funds, capacity, and reminders.
5. Fixed reports retain candidates, positions, funds, capacity, reminders, and explicit strategy-failure warnings.
6. Fixed pipeline failure remains explicit and cannot be rendered as normal no-candidate.
7. Manual/force remains snapshot-only and cannot create or send ordinary Daily Brief envelopes.
8. Query remains read-only, supports aggregate latest/day/revision views, uses exact nested heading levels, and does not expose revision internals in user text.
9. Adopt latest `main` mobile-flat output: flat title/status/market/data fields, flat candidate/position/funds lines, no blockquotes or nested lists.
10. Keep funds limited to cash total, option-opening funds, and candidate capacity; do not add total assets.
11. Update README and PRD examples to the final rendered contract.
12. Remove only stale tests whose assumptions contradicted the already-accepted manual/force no-send contract and obsolete assembler time fixtures; retain equivalent current-contract coverage.

## Changed Resolution Files

- `README.md`
- `docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md`
- `src/application/agent_tools/daily_brief.py`
- `src/application/daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_agent_tool.py`
- `tests/test_daily_decision_brief_notification_flow.py`
- `tests/test_daily_decision_brief_renderer.py`

## Protected Workspace Handling

Three untracked paths that became tracked on latest `main` were backed up before the merge and compared byte-for-byte with the checked-out `main` versions. All matched. Four unrelated older plan-review files remain untracked and untouched. No broad staging command was used.

## Review

Strict aggregate review artifact:

- `docs/reviews/code-review-20260721-231539.md`
- Conclusion: pass after fix/re-review
- Accepted finding `MR109-01`: stale close-advice assertion contradicted the merged mobile-flat contract — fixed and re-reviewed.
- Accepted finding `MR109-02`: generated dependency graph was stale after combining branches — regenerated and re-reviewed.
- Open accepted findings: none
- Blocking open questions: none

## Validation

Focused post-resolution validation:

- Daily Brief renderer, notification flow, query tool, scheduled notification, symbol notification, and multi-tick formatting: `98 passed`.
- Ruff on resolved source/test files: passed.
- Python compilation on resolved source files: passed.
- Conflict-marker scan: passed.
- Staged diff whitespace check before artifact creation: passed.

Broader validation recorded before this merge-fix artifact:

- Daily Brief + candidate trace + symbol monitoring: `197 passed`.
- Channel-aware notification focused suite: `145 passed`.
- Scheduler/multi-tick suite: `101 passed`.
- Agent/plugin/copilot suite: `158 passed`.

The first full repository run exposed two integration findings: one stale legacy close-advice assertion and stale generated dependency graph files. Both were fixed at their owning boundary and re-reviewed.

Final gates after the fixes:

- Full repository tests: `2973 passed, 10 skipped`.
- Ruff: passed.
- `compileall` for `src`, `domain`, and `scripts`: passed.
- Dependency graph check: passed with `production_modules=478`, `cycles=0`.
- US example YAML config validation: `ok=true`.
- HK example YAML config validation: `ok=true`.
- Staged diff whitespace check: passed before final artifact restaging.

## Documentation Decision

README and PRD examples were intentionally changed because user-visible formatting is part of the public notification/query contract. No additional workflow or configuration surface was introduced.

## Residual Risks

- Real provider rendering and scheduler timing remain unobserved in production — requiring separate explicit user approval after merge/release.
- Existing production delivery-pointer migration remains a separate approval-gated work unit.
- Revision retention remains deferred to measured operational need.

All residual risks are classified and non-blocking for merge.

## Production Boundary

This merge-fix work does not:

- release and remotely upgrade the consolidated Daily Brief scheduled-renderer version;
- release or deploy code;
- migrate lx/sy delivery pointers;
- trigger a real tick;
- send a real notification;
- modify production services, secrets, broker-facing state, positions, or trade events.

## Next Gate

Conclude the merge commit, push the feature branch, wait for PR #109 checks, perform final PR re-review/verification, and merge with a merge commit without deleting the branch.

## Artifact

`docs/gateflow/option-notification-experience-main-merge-fix-20260721.md`

## Second latest-main integration — renderer consolidation and v1.4.1

- Second merged base: `origin/main@ba6efbe9df943db62c19880f2733918d39f5f3c1` (`release: v1.4.1`).
- Latest-main authority retained: Daily Brief is the sole scheduled ordinary renderer; deprecated `daily_brief.enabled` values are ignored for routing; manual/force has no ordinary provider path; combined multi-market execution is terminal fail-closed before Daily Brief persistence/provider work.
- PR #109 behavior retained: one canonical scan; fixed complete reports; half-hour new-candidate alerts; successful current/read surfaces; funds without total assets; delivery v2 exact envelopes and delivery-only retry.
- Integration finding `MR109-INTEGRATION-03`: delivery-only `--no-send` falsely recorded sent — fixed with skipped/would-send semantics and pending-envelope regression coverage.
- Integration finding `MR109-INTEGRATION-04`: option plan/test claimed multi-market snapshot persistence contrary to latest terminal guard — plan/docs/tests corrected and re-PlanReviewed.
- Superseded PlanReview: `docs/reviews/plan-review-20260721-234559.md`.
- Accepted final PlanReview: `docs/reviews/plan-review-20260721-235237.md`.
- Final PR re-review: `docs/reviews/code-review-20260721-160110.md` — pass.
- Full repository tests: `2985 passed, 10 skipped`.
- Ruff, compileall, dependency graph (`479` modules, `0` cycles), US/HK config validation, conflict-marker scan, and whitespace checks: passed.
- Production boundaries unchanged: no release/deploy/config/service/pointer migration/tick/real notification action was performed.
