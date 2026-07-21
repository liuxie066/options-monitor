# Gateflow Final Closeout: Daily Brief Close-Position Details

## Gate

- Work unit: `daily-brief-close-details`
- Gate: final closeout
- Completion status: `final closeout pass`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/105`
- PR state at closeout: open draft, mergeable
- Issue link status: not applicable; this work unit was not opened from a numbered GitHub issue.

## What Changed

Daily Decision Brief position rows now carry and render the existing close-advice facts needed to make a close recommendation actionable:

- advisory reference close price from `close_mid`, explicitly labeled `(mid)`;
- signed estimated close P&L from `realized_if_close`;
- remaining annualized return from `remaining_annualized_return`.

The rendered detail is limited to explicit close actions. Non-negative P&L is labeled `预计锁定收益`; negative P&L is labeled `预计平仓损益`. Hold and unavailable rows suppress close metrics, including stale source values. Partial or malformed metrics degrade without inventing data.

## Scope Preserved

- No close-advice threshold, tier, reason, or action-selection change.
- No notification trigger, material-diff, routing, confirmation, or delivery-state change.
- No schema-version, config, runtime state, Feishu, ledger, broker, release, deployment, remote upgrade, or production canary change.
- The original dirty worktree was left untouched.
- Untracked `paseo.json` was never staged or included in the PR.

## Changed Production and Test Surfaces

- `src/application/daily_decision_brief_service.py`
- `src/application/daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_renderer.py`

Gate/review documentation and the generated dependency graph were also added/updated as required by repository policy.

## Verification

- Focused implementation tests: `68 passed`.
- Renderer/service/dependency-graph regression set: `39 passed`.
- Daily Brief tests excluding the expired fixture: `107 passed, 1 deselected`.
- Ruff on changed Python files: pass.
- Compile checks: pass.
- Dependency graph check: pass.
- `git diff --check`: pass.
- Full local suite: `1 failed, 2858 passed, 10 skipped in 42.58s`.
  - The only failure uses a fixture valid until `2026-07-20T20:00:00+00:00`; on July 21, 2026 the runtime correctly returns `planning_only`, while the test expects `live_actionable`.
  - This is an unrelated date-sensitive baseline and is assigned to a separate maintenance work unit.
- GitHub checks on accepted PR-review head `20c876e3`: `5 successful, 0 failing, 0 pending`.

## Docs Updates

- `docs/AGENT_WIKI.md` documents the advisory mid, signed P&L wording, remaining annualized return, and stale/unavailable suppression contract.
- `docs/DEPENDENCY_GRAPH.md` was regenerated and its check passes.
- Gateflow plan, implementation, aggregate review, fix/re-review, and PR review artifacts are committed.

## Finding Status

- Plan review finding `PR-001`: fixed before implementation; signed negative P&L is not mislabeled as profit.
- Slice code review: no findings.
- Aggregate deepreview `DR-001`: fixed; generated dependency graph is current.
- PR review: no findings.
- No deferred or unclassified code finding remains.

## Remaining Risks and Owners

- Date-sensitive expired fixture: separate maintenance work unit; repository maintenance owner.
- Production notification canary: later release/deployment work only with operator authorization.
- Advisory `(mid)` may not be executable: explicitly disclosed in output and documentation; operator retains execution judgment.

## Draft-PR-Pass Evidence

- Accepted plan commit: `70fc0485`.
- Accepted implementation slice commit: `a3a76e70`.
- Accepted aggregate deepreview commit: `517ba536`.
- Accepted PR review commit: `20c876e3`.
- PR review artifact: `docs/reviews/pr-105-review-20260721-103907.md`.
- Final accepted PR-review push completed and GitHub checks passed.

## Next Entry Point

The work unit is complete at `final closeout pass`. The next authorized action is human review and merge of draft PR #105. Merge, marking ready for review, requesting reviewers, release, deployment, and production canary remain outside this work unit.

## Artifact

`docs/gateflow/daily-brief-close-details-final-closeout-20260721.md`
