# Gateflow Goal Confirmation — Shadow Replay integrity selection

- Gate: goal confirmation
- Work unit: `om-shadow-replay-integrity-selection`
- Confirmed by user: 2026-08-01, explicit confirmation to implement, validate,
  merge, release, upgrade production, and run the controlled canary
- Base: `origin/main@8d467282`

## Goal and motivation

Repair the Strategy Lab sample path that spent OpenD capacity on legacy Shadow
Replay datasets and only then failed because those datasets lack verifiable
integrity manifests. The repair must select executable evidence before applying
the run limit and must fail direct write calls before external reads or cache
writes.

## Direct evidence

- The production receipt selected five `legacy_unverified` datasets and recorded
  five `dataset integrity receipt missing` errors with `executed_count=0`.
- `shadow_replay_dataset_status()` already owns dataset-integrity inspection via
  `validate_dataset_integrity(..., require_manifest=False)`.
- `_run_plan_rows()` previously applied `max_datasets` before any integrity
  eligibility check.
- `collect_shadow_replay_marks()` previously fetched OpenD required data before
  `mark_shadow_replay_dataset()` enforced integrity.

## Success signals

- Write-mode data plans record legacy datasets as explicit integrity skips and
  continue to verified datasets without consuming the execution budget.
- Direct write-mode collection rejects unverified datasets before OpenD or
  required-data/cache mutation.
- Historical evidence is not rewritten or backfilled.
- Tests, architecture checks, release verification, remote readback, and a real
  production Strategy Lab canary pass.

## Non-goals and boundaries

- No manifest synthesis or migration for historical datasets.
- No change to dry-run readability or status inspection.
- No runtime-config, trade-state, notification, Feishu, broker, or order writes.
- No redesign of Shadow Replay integrity format, provider rate limiting, or
  dataset ordering beyond eligibility filtering.

## Blocking open questions

- None. The user confirmed the implementation, merge, release, production
  upgrade, and controlled canary boundaries.

## Decision

Goal confirmation passed. Next entry point: plan.
