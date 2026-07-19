# Fix — Daily Decision Brief S4

- **Gate**: fix
- **Work unit**: `daily-decision-brief`
- **Slice**: S4 — CLI, Agent Tool, config and docs
- **Date**: 2026-07-19
- **Target finding**: `CR-S4-1`
- **Status**: implementation complete; pending re-review
- **Artifact path**: `docs/gateflow/daily-decision-brief-s4-fix-20260719.md`

## Fix

### CR-S4-1 — 已修复

The shared read view now returns the accepted minimal structured contract for both available and unavailable state:

- `coverage`: status/reason plus action, position, data-gap and source-artifact counts;
- `source`: canonical source label plus `state_path` masked through `mask_path`;
- `freshness`: data-as-of, valid-until and effective actionability;
- handler meta also carries only the masked state path.

Unavailable and state-invalid results do not expose repository absolute paths or raw state errors. Existing stored `brief` remains unchanged, and effective actionability/rendered Markdown behavior is unchanged.

`_OUTPUT_CONTRACT` now declares the added public fields and structured freshness paths.

## Regression evidence

- available persisted brief reports structured coverage/source/freshness and no absolute workspace path;
- not-found result reports unavailable coverage/freshness plus masked current-state path;
- simulated `state_invalid` result masks the source path and omits the raw repository error containing the absolute path;
- manifest remains pure read and side-effect free;
- focused Agent/CLI/agent-contract suite: `109 passed`.

## Residual risks

- The source path intentionally identifies only the final filename (`.../<name>`), not account directory hierarchy; this follows the existing `mask_path` contract and avoids path disclosure.
- Historical state remains explicitly unavailable rather than migrated.
- No unclassified residual risk.

## Gate transition

- **Current gate**: S4 re-review.
- **Next entry point**: re-run deepreview against `b3c405c6`, verify CR-S4-1 status, then rerun full S4 validation.
