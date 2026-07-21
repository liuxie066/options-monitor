# Gateflow Final Closeout — Candidate Event Risk Monitoring

## Work Unit

- Name: `candidate-event-risk-monitoring`
- Date: `2026-07-21`
- Branch: `codex/candidate-event-risk-monitoring-v1.4.0`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/104`
- Release metadata: `1.4.0`
- Status: `final-closeout-pass` after this artifact is committed and pushed

## What Changed

- Made the current run's `output_runs/<run_id>/state/event_snapshot.json` the sole Daily Brief event authority; candidate CSV compatibility event fields are never a fallback.
- Added provider/category coverage evidence and fail-closed normalization so missing, malformed, stale, partial, conflicting, unsupported, and empty fallback evidence cannot become confirmed absence.
- Added one candidate-bound event-risk projection for Sell Put, Covered Call, and Combo Yield candidates, including nearest important event, days to event, evidence reliability, attention-window membership, and expiry relation.
- Evaluated Combo Yield against both Put and Call expirations while preserving the existing candidate/action identity, ranking, labeled-only authority, eligibility, and capacity.
- Added material transitions for event addition, date change, entry before expiration, evidence degradation, evidence recovery, and same-chain confirmed removal.
- Reused the existing last-confirmed Daily Brief comparison, material-only delivery decision, full-current-snapshot renderer, sender, and confirmation pointer.
- Added concise Chinese event decision lines and candidate-specific change summaries without exposing raw reason codes, provider identifiers, evidence-chain IDs, or internal enums.
- Preserved silence for freshness/cache-only updates and prevented provider degradation from being announced as event removal.
- Hardened Futu split pagination so a configured page-limit truncation records partial coverage instead of authoritative completeness.
- Updated README, Agent Wiki, dependency graph, VERSION, and CHANGELOG for release `1.4.0`.

## Verification

- Full Python 3.12 pytest after aggregate fix: `2893 passed, 10 skipped`.
- Focused Daily Brief/Event plus Agent contract/smoke suite: `247 passed`.
- Aggregate pagination regression suite: `38 passed`.
- Ruff on all touched Python and test files: passed.
- Dependency graph check: passed; `production_modules=477`, no production cycles or boundary violations.
- Release metadata check for `v1.4.0`: passed.
- US/HK example YAML config validation: passed.
- `git diff --check`: passed.
- GitHub Actions on accepted PR-review head `b816f602`:
  - Analyze (actions): passed.
  - Analyze (python): passed.
  - Agent Plugin: passed.
  - Guardrails: passed.
  - CodeQL: passed.

## Documentation Decision

- Public docs describe the three user event states, run-snapshot authority, no candidate CSV fallback, expiry relations, Combo Yield dual-leg handling, six material transitions, freshness silence, provider-degradation safety, default-off behavior, and non-goals.
- Public inputs are unchanged: no new config key, migration, CLI, Timeline, state machine, receipt, sender, renderer, scheduler, or publication workflow was added.
- VERSION and CHANGELOG agree on `1.4.0` dated `2026-07-21`.

## Review Findings

- Slice A `SA-01`: malformed structured event payload could be accepted as an empty successful event list — **fixed and re-reviewed**.
- Aggregate Deepreview `ADR-01`: truncated Futu split pagination could overclaim complete coverage — **fixed and re-reviewed**.
- Slice B, Slice C, Slice D, and PR #104 Deepreviews: **no substantive findings**.
- Open accepted findings: none.
- Deferred unclassified findings: none.

## Remaining Risks / Owners

- Live provider/API truth was not exercised. Owner: operator; requires separately authorized read-only/no-send provider canary.
- No real notification was sent. Owner: operator; any live Daily Brief canary/send requires explicit authorization.
- PR #104 is Draft and unmerged. Owner: user; mark-ready, reviewer request, approval, and merge remain outside this Gateflow authorization.
- GitHub tag/Release `v1.4.0` does not exist until this VERSION change is merged to `main` and the VERSION-driven release workflow succeeds. Owner: release workflow/operator.
- Production `liuxie-incus` remains unchanged. Owner: operator; remote preflight must read remote `AGENTS.md`, verify disk/config/service state, and obtain explicit confirmation before `om update apply --confirm` or service mutation.
- `notifications.daily_brief.enabled` remains `false`; merging or upgrading will not enable Daily Brief or send a notification.

## Draft PR / Issue Status

- Draft PR: `https://github.com/liuxie066/options-monitor/pull/104`
- PR state at closeout: open and draft.
- PR review artifact: `docs/reviews/pr-104-review-20260721-102348.md`.
- Issue link: not applicable; this work unit was not opened from a GitHub issue.
- Requested reviewers/comments: none.

## Next Entry Point

1. User reviews PR #104 and explicitly authorizes mark-ready/merge if desired.
2. After merge, verify the VERSION-driven GitHub tag and Release `v1.4.0` plus release workflow checks.
3. Perform a separately authorized read-only/no-send provider and Daily Brief canary.
4. Perform read-only remote upgrade preflight on `liuxie-incus`, including remote instructions and disk headroom.
5. Request explicit confirmation before remote apply/service mutation, then verify remote version, config, and services without using a real notification as the first canary.
