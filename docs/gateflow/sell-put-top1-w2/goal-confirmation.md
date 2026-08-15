# Gateflow Goal Confirmation — Sell Put Top1 W2

- Gate: `goal confirmation`
- Work unit: `sell-put-top1-w2`
- Branch: `feat/sell-put-top1-w2`
- Base: `origin/main@9b29e05b`
- Design documents:
  - `docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md`
  - `docs/plans/sell-put-top1-modular-technical-implementation-plan-20260814.md`
  - `docs/plans/sell-put-top1-modular-implementation-control-20260814.md`
- Confirmation: the user approved the modular implementation documents and explicitly requested sequential Gateflow implementation with Kimi DeepReview after every module.
- Artifact path: `docs/gateflow/sell-put-top1-w2/goal-confirmation.md`

## Goal and motivation

Publish one immutable `recommendation_point.v1` audit fact for each official scheduled Sell Put recommendation decision. This gives later corpus and experiment modules a producer-owned point identity without making the production tick depend on an experiment database or experiment state.

## Success signals

- A scheduled account pipeline with a committed scheduler watermark, valid terminal candidate manifest, and valid current opening snapshot publishes the canonical run/account point artifact.
- Repeating the same point is idempotent; different bytes at the same run/account path fail closed.
- The point binds the canonical scheduler target, terminal manifest, opening snapshot, producer accepted Sell Put IDs, policy/config hashes, decision clock, and source commit.
- W1A validates the clean point-to-opening-snapshot binding and accepted candidate IDs immediately.
- Manual, force, smoke, replay, delivery-only, missing-target, failed-pipeline, or maintainer-disabled paths do not publish.
- Any observer failure occurs only after a successful watermark commit and cannot change tick, watermark, or notification behavior.
- Focused tests, architecture guard, Ruff, dependency graph, and available type checks pass before Kimi DeepReview.

## Scope boundary

### Included

- `recommendation_point.v1` builder, strict validator, binding projection, loader, and run/account-scoped write-once publisher.
- Exact `OM_STRATEGY_LAB_TOP1_AVAILABLE == "1"` process-environment read.
- Minimal best-effort observer between scheduler watermark commit and notification delivery.
- Reuse of the existing terminal candidate bundle loader, opening snapshot contract, W1A projection contract, and safe write-once state writer.
- A shared production source-commit resolver only if the existing ledger-owned implementation must be reused by W2 without importing ledger migration code.

### Excluded

- Experiment SQLite, account opt-in, corpus capture, research, validation, outcomes, timers, CLI, Agent tools, LLM, and Prompt work.
- A generic feature-flag service, observer framework, event bus, repository interface, registry, or retry/backfill mechanism.
- Candidate Engine changes, filtering/ranking changes, production configuration changes, release, deployment, service installation, notification sends, or real experiments.

## Direct code evidence

- `src/application/tick_notification_flow.py::run_tick_notification_flow()` already commits scheduler scan targets through `_commit_scan_targets_before_delivery()` before provider delivery; this is the single required observer seam.
- `src/application/tick_account_execution.py` already exposes the accounts whose pipelines ran and their account-scoped scheduled targets.
- `src/application/candidate_snapshot_manifest.py::load_candidate_snapshot_bundle()` validates the terminal manifest, status index, exact owner files, and opening snapshot bindings.
- `src/application/tick_run_workspace.py::write_account_run_state_bytes_once_safely()` already provides byte-level write-once/adopt/conflict semantics at the required run/account state path.
- `src/application/strategy_lab/top1/ranking.py::build_ranking_projection()` already validates the W1A point binding, current opening snapshot, producer accepted IDs, and baseline parity.
- The only existing release-aware clean source-commit resolver is private to ledger migration, so W2 must either extract that exact behavior to a narrow shared helper or fail to publish in installed release directories.

## First-principles and overdesign judgment

The point must exist because later modules cannot reconstruct an official scheduler decision after the source run is retained or deleted. One audit file and one observer call are sufficient. No persistent point database, automatic retry, backfill, generic flag system, or experiment orchestration is needed in W2.

## Dirty-worktree ownership

The primary worktree and all unrelated worktrees remain untouched. W2 owns only the isolated worktree `/private/tmp/options-monitor-sell-put-top1-w2-20260815` and its branch.

## Blocking open questions

None.

## Residual risks

- Source-run retention and long-lived corpus copying are assigned to W4.
- Account opt-in and effective two-layer feature gating are assigned to W3 and later feature surfaces; the producer observer reads only the maintainer availability gate.
- Runtime service/profile rendering of the new availability variable is assigned to W7; W2 only consumes the already-loaded process environment.
- Real pilot/provider readiness remains governed by W0R and does not block this synthetic producer seam.

## Decision

`goal-confirmation-pass`; next gate: `plan`.
