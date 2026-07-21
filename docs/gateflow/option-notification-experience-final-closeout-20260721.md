# Gateflow Final Closeout — Option Notification Experience

## Gate

- Work unit: `option-notification-experience`
- Gate: final closeout
- Completion status: `final closeout pass`
- Branch: `feature/option-notification-experience`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/109`
- Accepted PR-review head: `2c40ca38ae63ab7eb3b62001ce9258dabc0510e2`
- PR state at closeout: open draft; base `main`; head `feature/option-notification-experience`
- Issue link status: not applicable; this work unit was not opened from a numbered GitHub issue.

## What Changed

The notification contract is now predictable and uses one canonical strategy pipeline:

- The existing 10-minute scheduler wake-up remains, but the strategy pipeline runs only at `09:40`, valid whole-hour fixed points, valid half-hour candidate-check points, and `15:50`; `09:30`, lunch breaks, and other wakes do not scan.
- Fixed points send a complete account report even when there is no candidate.
- A valid half-hour successful scan sends immediately when it discovers a candidate identity that has not been confirmed before that day.
- If fixed-report and new-candidate conditions are both true, one complete fixed report is sent; no duplicate second message is generated.
- Each later confirmed candidate batch may advance to a new exact delivery envelope, while pending exact retry and ambiguous-send freezing retain their original safety contracts.
- Delivery-only retries reuse the persisted envelope without broker fetch, a second scan, a new revision, or message rotation.

The user-facing report and read path were also completed:

- `./om daily-brief latest` and `daily_decision_brief_read` return the latest reliable successful account/market snapshot without scanning, sending, confirming, or changing delivery state.
- Reports include current candidates, positions, cash total, option-opening funds, candidate-scoped capacity, data time, and freshness.
- Funds deliberately exclude total assets, NAV, and securities market value. Unknown funds are explicit and are never rendered as zero.
- Markdown keeps internal revision, digest, delivery key, pointer, identity, raw enum, path, and broker-code details out of the user message.

Failure handling is fail-closed:

- Sell Put, Covered Call, and Combo Yield execution failures write run/account-scoped structured failure evidence.
- Candidate rows from a failed strategy family are discarded even if a stale or partial CSV remains.
- When no reliable candidate family remains, the brief is `blocked` and a fixed point sends an explicit failure report instead of saying there are normally no candidates.
- When another family remains reliable, its candidates stay visible and the brief is `degraded` with a concise incomplete-result warning.
- Controlled successful empty artifacts without failure evidence remain normal authoritative empty results.

## Verification

Final local verification on the accepted PR-review code:

- Daily Brief, candidate trace, and symbol monitoring focused suite: `197 passed`.
- Scheduler and multi-tick focused suite: `100 passed`.
- Full repository: `2952 passed, 10 skipped in 44.95s`.
- `python3.12 -m ruff check .`: pass.
- `python3.12 -m compileall -q src domain scripts`: pass.
- Dependency graph: current; `478` production modules; `0` production cycles; boundary pass.
- US example YAML config validation: `ok=true`.
- HK example YAML config validation: `ok=true`.
- `git diff --check`: pass.

GitHub checks on accepted PR-review head `2c40ca38`:

- Analyze (actions): passed.
- Analyze (python): passed.
- CodeQL: passed.
- agent-plugin: passed.
- guardrails: passed.

## Documentation Decision

- `docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md` and the accepted implementation plan record the product and architecture contract.
- `README.md` and `docs/AGENT_WIKI.md` document the fixed/half-hour schedule, one-pipeline rule, complete-report behavior, latest read surfaces, funds fields, delivery inspection/migration commands, Daily Brief sole scheduled-renderer authority, and separately gated production rollout.
- `docs/INDEX.md` and generated dependency artifacts were updated.
- Gateflow goal, plan-review, implementation, code-review, aggregate deepreview, PR-review, fix, re-review, and closeout evidence are retained in the repository.

## Finding Status

- Plan review: three review rounds were repaired and the final plan was accepted before implementation.
- Slice reviews: accepted findings were fixed and re-reviewed before each slice was committed.
- Aggregate deepreview: accepted findings were fixed and re-reviewed; accepted deepreview commit is `c78daf7d`.
- PR finding `PR109-01` — only the first same-day candidate batch could send: fixed and re-reviewed. Later distinct confirmed candidate batches now receive a new delivery envelope while historical confirmation evidence is preserved.
- PR finding `PR109-02` — strategy failure could appear as normal no-candidate: fixed and re-reviewed through dedicated strategy-failure evidence, failed-family row rejection, and blocked/degraded user projection.
- Accepted PR-review commit: `2c40ca38`.
- Open accepted findings: none.
- Deferred unclassified findings: none.

## Remaining Risks and Owners

- Half-hour checks are scheduled monitoring, not real-time market alerts. Normal discovery is at the next valid half-hour or fixed point plus pipeline/send latency. Owner: product contract; no second scanner is planned.
- Every reliable scheduled scan creates an immutable revision. Current volume is accepted; retention should be added only if observed storage growth exceeds the runtime budget. Owner: later maintenance work unit if measured.
- The JSONL strategy-failure artifact assumes the current run/account directory ownership model. If multiple processes later share the same writer scope, add locking or a structured state repository then. Owner: later architecture work only if concurrency changes.
- Live provider behavior, production capacity, real notification delivery, and normal-schedule timing have not been exercised by this work unit. Owner: separately authorized rollout/canary.
- Draft PR #109 is unmerged. Owner: user; ready-for-review, reviewer request, approval, and merge remain outside this Gateflow authorization.

## Production Rollout Boundary

This work unit did not:

- release and remotely upgrade the version that makes Daily Brief the sole scheduled ordinary-notification renderer;
- inspect or migrate the production lx/sy delivery pointers;
- send a real notification or manually trigger a real tick;
- change a service or production runtime config;
- bump `VERSION`, create a release, or upgrade the remote runtime.

The later production path remains separately approval-gated: merge and release the code, perform read-only remote/config/pointer/capacity inspection, dry-run the lx/sy pointer migration, obtain explicit approval for production config and migration writes, avoid a manual tick, and observe the next normal scheduler targets and the read-only latest-query result.

## Safety / External Actions

- No production config, service, secret, ledger, option-position state, trade event, broker-facing data, or runtime artifact was modified.
- No live notification was sent.
- PR #109 remains draft; it was not approved, marked ready, merged, commented on, or assigned reviewers.
- The unrelated untracked plan/review files present in the original worktree were not staged or modified.

## Draft-PR-Pass Evidence

- Accepted plan commit: `ae52a0d3`.
- Accepted implementation slice commits: `bb24f87b`, `a8f4aeb3`, `55551b10`, `1c116940`, `804410fa`, `29850406`, `fd59be58`.
- Accepted aggregate deepreview commit: `c78daf7d`.
- Accepted PR review commit: `2c40ca38`.
- PR review artifacts: `docs/reviews/pr-109-review-20260721-210448.md` and `docs/reviews/pr-109-review-20260721-212559.md`.
- Final accepted PR-review push completed and all GitHub checks passed.

## Next Entry Point

The work unit is complete at `final closeout pass`. The next action is user review and explicit authorization to mark ready or merge draft PR #109. After merge, release/deployment and production enablement/migration/normal-schedule observation are separate controlled work units with explicit approval before any write or real send.

## Artifact

`docs/gateflow/option-notification-experience-final-closeout-20260721.md`
