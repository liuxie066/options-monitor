# Gateflow Final Closeout — HK Terminal Fee Contract

## Gate

- Work unit: `sell-put-top1-hk-terminal-fee-contract`
- Gate: `final closeout`
- Date: `2026-08-15`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/157`
- Base: `main@8528de6b`
- Accepted plan: `8b879390`
- Accepted implementation: `075f3659`
- Accepted aggregate review: `c0f6e1dd`
- Accepted PR review: `f6361f0f`
- Verified draft-PR-pass: `61ace4a5`

## What changed

1. Added one versioned pure HK terminal-fee calculator for assignment,
   exercise, and expired-worthless outcomes.
2. Reused the existing seven-component HK stock-fee arithmetic; assignment has
   zero exercise fee, exercise adds HKD 2 per contract, and expired-worthless
   has a sourced complete zero policy.
3. Kept actual broker fee evidence authoritative and required strict account
   plan facts before an estimated terminal fee can be treated as complete.
4. Updated assigned-stock lifecycle and portfolio assignment scenario to keep
   missing plan-bound fees fail closed while retaining named audit estimates.
5. Propagated missing lifecycle net economics through aggregate net and
   annualized-efficiency outputs instead of silently treating them as zero.

## What was verified

- Focused fee and consumer tests: `45 passed`.
- Adjacent money-path and Strategy Lab regressions: `329 passed`.
- Full repository: `4754 passed, 10 skipped`; the only sandbox loopback-bind
  failure passed in the exact sandbox-external rerun.
- Ruff, dependency graph (`577` production modules, `0` cycles), guardrails,
  and patch checks passed.
- Corrected slice re-review, aggregate DeepReview, and PR-level Kimi
  DeepReview passed with no unresolved finding.
- GitHub Analyze Actions, Analyze Python, CodeQL, agent-plugin, and guardrails
  checks passed on both the accepted PR-review and draft-PR-pass heads.
- PR #157 remained `OPEN`, `DRAFT`, `MERGEABLE`, and `CLEAN`; it was not moved
  to Ready for review.

## Docs decision

- Updated the Top1 capability preflight to distinguish a locked domain fee
  contract from runtime readiness.
- Added complete Gateflow, review, readiness, draft-pass, and closeout evidence.
- No CLI or operations documentation changed because this work unit adds no
  CLI, provider, configuration, service, or runtime surface.

## Finding status

- `DR-HKF-01`: closed as a false positive after exact-input reproduction.
- `DR-HKF-02`: fixed and re-reviewed; string platform fees fail closed.
- `ROOT-HKF-01`: fixed and re-reviewed; aggregate net outputs preserve missing
  economics.
- Aggregate and PR review: no findings.
- Open, deferred, or unclassified finding in this work unit: none.
- Review artifact `034739` is explicitly superseded audit history; effective
  conclusions are recorded by `035048`, `035941`, and `040649`.

## Remaining risks and owners

- Real `lx` fee-plan receipt and validated intake: later W0R/provider work unit;
  provider-dependent research and a real pilot remain no-go until closed.
- OpenD, quota, calendar, K-line, observation, and terms-capacity gaps: their
  existing later W0R work units.
- End-to-end exercise event ingestion: later lifecycle/provider work.
- Fee schedule changes: a new versioned schedule work unit, never mutation of
  `futu_hk_terminal_fee.v1` history.

## Safety and workspace status

- No merge, Ready-for-review transition, release, tag, deployment, remote
  upgrade, service/config mutation, provider call, notification, trade, ledger
  write, broker action, or other production write was performed.
- Work occurred in an isolated worktree. The root worktree's unrelated user
  changes and the named recovery stashes remain untouched.
- This work unit is not bound to a GitHub issue, so no issue closing keyword or
  issue closeout comment applies.

## Completion and next entry point

This work unit is complete at `final closeout pass`, subject only to the
mechanical GitHub checks for this closeout-documentation commit.

Next entry point: confirm the W1B work-unit goal and its branch dependency on
the still-unmerged fee-contract PR before implementation. Merge, release, and
deployment remain separate explicit decisions.
