# Gateflow Final Closeout — Candidate Brief Evidence Integrity

## Gate

- Work unit: `candidate-brief-evidence-integrity`
- Gate: `final closeout`
- Date: `2026-08-12`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/149`
- Base: `main@b85607be` (`1.13.13`)
- Accepted product/PR-review merge commit: `3ce14a58462b41cb7982516566473c39c015a3bd`
- Accepted supplemental PR-review commit: `e73cba87cdede864e4c01d8cce738ab2e6fc57dc`
- Verified `draft-PR-pass` commit: `e64b1512cec1283c1ee88466d78a6485c357115b`

## What Changed

1. Opening-ready `net_premium_non_positive` outcomes are deterministic policy rejections, while missing readiness,
   quote, binding, and realized-volatility evidence remain unresolved and fail closed.
2. Nested sealed candidate causes, including `term_matched_rv_unavailable`, propagate through the opening snapshot
   into the Daily Brief and retain specific user-facing RV wording.
3. Prefetch status `fetched` is treated as success.
4. The CC+LP sealed snapshot is required only when the effective current-market configuration enables `cc_lp`,
   including template/profile expansion.
5. AI-unavailable copy uses canonical pre-budget candidate presence. A strategy family with candidates keeps the
   raw-ranking fallback; an empty family explicitly says there is no displayable ranking.
6. The latest `main` compact fixed-report path is integrated without suppressing recognized sealed RV gaps or
   collapsing independent Sell Put / Covered Call facts.
7. Gateflow and DeepReview evidence uses portable repository-relative development commands and passes the sensitive
   path guardrail.

## What Was Verified

- Clean clone at accepted product/PR-review commit `3ce14a58`:
  - core candidate / Daily Brief / renderer suite: `160 passed`;
  - related AI Advice, Daily Brief, CC+LP, and Combo Yield suite: `400 passed`;
  - compileall: passed;
  - Ruff on changed source/tests: passed;
  - `git diff --check`: passed.
- Post-CI-fix tree:
  - repository-wide Ruff: passed;
  - CI-equivalent guardrails command: passed;
  - GitHub Agent Plugin and Guardrails passed on `85b885be`, `e73cba87`, and `e64b1512`.
- GitHub PR at `e64b1512`: open, Draft, mergeable, base `b85607be`.
- HK/US and Sell Put/Covered Call symmetry is covered by the focused and related regression suites.

## Documentation Updates

- `docs/AI_DECISION_ADVICE_DESIGN.md`: documents specific sealed hard-evidence reminders and per-family candidate
  presence under a global AI-unavailable notice.
- `docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md`: aligns the compact scheduled report with the same evidence and
  strategy-family boundaries.
- Gateflow and DeepReview artifacts record goal confirmation, plan review, both implementation slices, aggregate
  review, full PR review, CI correction, incremental re-review, and Draft PR pass.

No public schema, CLI, runtime config key, strategy threshold, ranking rule, capacity rule, persistence contract, or
provider contract changed.

## Finding Status

- Plan review findings: all accepted findings fixed; re-review passed.
- Slice 1: CR-S1-01 and CR-S1-02 fixed; re-review passed.
- Slice 2: CR-S2-01 fixed; re-review passed.
- Aggregate DeepReview: CR-AGG-01 fixed; re-review passed.
- Full PR review: CR-PR-01, CR-PR-02, and CR-PR-03 fixed; re-review passed.
- PR CI: CI-GR-01 fixed; incremental DeepReview passed.
- Open or unclassified finding in this work unit: none.

## Remaining Risks / Owners

- Production/runtime replay and scheduled delivery proof: owner/destination = separately authorized release or
  remote-upgrade verification. Source delivery does not authorize either operation.
- Manual symbol-subset CC+LP config propagation: owner/destination = later work unit.
- Future additions to the definitive calculation-reason taxonomy: owner/destination = later work unit with
  independent contract evidence and regressions.

These risks are outside the confirmed work-unit goal and do not block the source-only Draft PR.

## Issue Link Status

This is not an issue-driven work unit. No closing keyword, issue mutation, or issue closeout comment is required.

## Safety / Workspace Status

- No release, tag, deployment, remote upgrade, service change, runtime write, notification replay, merge, approval,
  reviewer request, or Ready-for-review transition was performed.
- The `1.13.13` version metadata is inherited from integrated upstream `main`; this branch did not publish it.
- Implementation occurred in an isolated clone. The primary workspace and its unrelated tracked/untracked user
  changes were not staged, overwritten, reset, stashed, or committed.

## Completion Status / Next Entry Point

The work unit is complete at `final closeout pass`, subject only to the mechanical GitHub checks for this closeout
document commit. Those checks do not alter the accepted product tree and are verified before the final handoff.

Next entry point: the user may review the Draft PR and explicitly choose a Ready-for-review or merge workflow. Release,
deployment, and remote upgrade remain separate later authorizations.
