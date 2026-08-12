# Gateflow Final Closeout — Retire AI Decision Advice

## Gate

- Work unit: `retire-ai-decision-advice`
- Gate: `final closeout`
- Date: `2026-08-12`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/150`
- Base: `main@beb0836562c269e78a5f540659d20f76ee71e3d0`
- Accepted PR-review commit: `3b0c2aa68bcd4c16f04219ce0287f6f5e33c3eef`
- Verified `draft-PR-pass` commit: `aa2a0ebd8200a8232e163931aac16b135f20504e`

## What Changed

1. Removed AI Decision Advice generation, prompts, validation, orchestration, evidence storage and Agent enrichment.
2. Removed external-news collection, its internal CLI, dedicated DeepSeek Responses adapter and managed Collector
   service/timer generation.
3. Removed Advice-only portfolio-distribution preparation and its specialized client helpers while preserving generic
   portfolio views and the rest of the portfolio-management client.
4. Removed Advice from current Daily Brief assembly, normalization, material diffs, text/card rendering, candidate
   expansion and Agent read output.
5. Preserved immutable overlay-era Brief digest validation without normalizing or exposing the retired fields.
6. Added fail-closed classification for pending or ambiguous frozen notifications containing legacy Advice source,
   text or card payloads; those envelopes are not sent or advanced, while clean accounts remain independent.
7. Retired the exact YAML/runtime config key with a targeted error and removed Collector/Tick credential consumers;
   Assistant-owned DeepSeek credentials remain available.
8. Updated current operator/product documentation and regenerated dependency artifacts. Historical release/review
   records remain untouched.
9. Integrated latest-main candidate evidence-integrity behavior, including the sealed term-matched RV hard-evidence
   reminder, without restoring any Advice-specific copy or implementation.

## What Was Verified

- Latest-main focused integration suite: `485 passed`.
- Sandbox-safe full suite: `4459 passed, 10 skipped, 5 existing deprecation warnings`.
- Localhost-only HTTP quality suite: `4 passed`.
- Repository-wide Ruff: passed.
- Compileall across `domain`, `src` and `tests`: passed.
- Dependency graph: current, `production_modules=568`, `cycles=0`.
- US/HK example config validate and build dry-runs: passed.
- Active production imports of deleted Advice/Collector/provider/preparation paths: none.
- Latest-main DeepReview and full PR review: passed with no actionable finding.
- GitHub Agent Plugin, Guardrails and all CodeQL checks passed on accepted review head `3b0c2aa6` and verified
  Draft-PR-pass head `aa2a0ebd`.
- GitHub PR at `aa2a0ebd`: open, Draft, mergeable, `CLEAN`, based on `beb08365`.

## Documentation Updates

- `docs/AI_DECISION_ADVICE_DESIGN.md` is now a concise retirement record with explicit retained-authority and
  production-cutover boundaries.
- `docs/AGENT_WIKI.md`, `docs/DEPLOY_LINUX_MAC.md`, `docs/SECRET_STORAGE.md`,
  `docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md`, example config and `CHANGELOG.md` match the source retirement.
- Gateflow and review artifacts record goal confirmation, adversarial plan review, both implementation slices,
  aggregate review, latest-main propagation, full PR review and Draft PR pass.

## Finding Status

- Plan review findings: fixed; re-review passed.
- Slice 1 findings: fixed; re-review passed.
- Slice 2 finding: fixed; re-review passed.
- Aggregate findings: fixed; re-review passed.
- Latest-main propagation review: no actionable finding.
- Full PR review: no actionable finding.
- Open or unclassified finding in this work unit: none.

## Remaining Risks / Owners

- Deployed `ai_decision_advice` config and installed Collector service/timer units remain operational state. Owner and
  next destination: a separately authorized production reconcile with read-only drift first.
- Historical Advice/evidence artifacts remain on disk. Owner and next destination: a separately authorized,
  recoverable and explicitly targeted data-cleanup work unit if deletion is wanted.
- Unknown external private consumers of deleted internal modules are outside repository visibility. Owner: external
  consumer; the accepted design deliberately provides no compatibility shim.
- Source retirement does not prove production cutover until a later release/upgrade and runtime verification are
  explicitly authorized.

These are outside the confirmed source-only goal and do not block the Draft PR.

## Issue Link Status

This is not an issue-driven work unit. No closing keyword, issue mutation or issue closeout comment is required.

## Safety / Workspace Status

- No merge, Ready transition, reviewer request, approval, release, tag, deployment, remote upgrade, service/config/
  secret mutation, runtime data write, notification send or historical deletion was performed.
- Work occurred in the isolated branch worktree. The primary worktree remains on its existing `main` and has ongoing
  unrelated ledger, Futu gateway, settlement-test and generated dependency-document changes; none were staged,
  reset, restored or transferred into this branch.
- Protected stash `codex-preserve-before-main-us-notification-20260812` remains present with exact patch hash
  `9ba116779673b0d5485d4ea5cc29cee0950e9b72c972627b395a23cb435de87f`.

## Completion Status / Next Entry Point

The work unit is complete at `final closeout pass`, subject only to the mechanical GitHub checks for this closeout
document commit. Those checks do not change the accepted product tree and are verified before final handoff.

Next entry point: the user may review Draft PR #150 and explicitly choose a Ready-for-review or merge workflow.
Release, production reconcile, historical cleanup and remote upgrade remain separate later authorizations.
