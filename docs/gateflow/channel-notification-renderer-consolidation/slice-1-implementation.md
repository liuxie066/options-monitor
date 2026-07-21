# Gateflow Implementation Artifact — Slice 1

- Gate: `implementation`
- Work unit: `channel-notification-renderer-consolidation`
- Slice: `1 — scheduled authority, trigger and failure state`
- Date: `2026-07-21`
- Base commit: `f1233e9f`
- Artifact path: `docs/gateflow/channel-notification-renderer-consolidation/slice-1-implementation.md`
- Status: `implementation complete; pending code review`

## Objective and outcome

Slice 1 makes Daily Decision Brief the only scheduled ordinary notification renderer, disables ordinary auto-delivery for manual/force runs, makes multi-market delivery fail closed before Daily Brief persistence or provider work, scopes tick idempotency by trigger kind, and moves old renderer config keys into Phase A accepted-with-warning compatibility.

Outcome:

- `trigger_kind=scheduled` is the only path that calls `_prepare_daily_brief_notification()`.
- `manual` and `force` finish normally with `non_scheduled_ordinary_notification_disabled`; they do not resolve a delivery route, create Daily Brief revisions, or attempt ordinary provider delivery.
- Multi-market scheduled delivery terminates with `daily_brief_multi_market_delivery_unsupported`, CLI return code `2`, error run-end state, and terminal `unsupported_failed` idempotency state.
- Compact/Legacy scheduled preparation, standalone no-candidate heartbeat preparation, and their private preparation DTO/functions were removed.
- Scheduled pipeline compatibility output is always Compact and is labeled as compatibility-only, not delivery evidence.
- Deprecated `notifications.daily_brief.enabled` and known `notifications.render_style=compact|legacy` values validate with stable warning codes but have no sender authority.
- Scheduled validation cache identity now includes `validator_version=notification-renderer-v2`.

## Changed ownership boundaries

### Notification authority

- `src/application/tick_notification_flow.py`
  - scheduled-only Daily Brief preparation;
  - manual/force ordinary notification finalization;
  - fail-closed multi-market terminal state;
  - fixed `tick_metrics.daily_brief.renderer=daily_brief`.
- `src/application/scheduled_notification.py`
  - retained delivery execution types/helpers;
  - removed old Compact/Legacy preparation and heartbeat helpers.
- `domain/domain/multi_tick_result.py`, `domain/domain/__init__.py`
  - removed dead multi-account message builders;
  - retained standalone compatibility no-candidate text preview;
  - generalized no-account last-run payload reason/error fields.

### Finalization and idempotency

- `src/application/multi_tick_finalization.py`
  - existing finalizer now accepts caller-provided reason, run-end outcome, error code, and return code;
  - default no-account behavior remains the default contract.
- `src/application/tick_run_context.py`, `src/application/multi_account_tick.py`
  - normalized `scheduled|manual|force` enters the idempotency key and records;
  - completion supports `ok` and `error_code`;
  - duplicate `unsupported_failed` records replay return code `2` without rerunning the pipeline.

### Config and compatibility artifact

- `src/application/config_validator.py`
  - stable `NOTIFICATIONS_DAILY_BRIEF_ENABLED_DEPRECATED` and `NOTIFICATIONS_RENDER_STYLE_DEPRECATED` warnings;
  - unknown/wrong-type render styles fail closed.
- `src/application/config_loader.py`
  - validator cache version bumped to `notification-renderer-v2` and compared alongside config SHA.
- `src/application/config_defaults.py`, `configs/system.json`, `configs/examples/user.common.example.json`
  - deprecated `daily_brief.enabled` removed from canonical defaults/examples.
- `src/application/pipeline_runtime.py`, `src/application/pipeline_alert_steps.py`
  - compatibility bundle always uses Compact;
  - logs explicitly say the bundle is not delivery evidence.
- `src/interfaces/cli/run_ops.py`, `README.md`, `docs/AGENT_WIKI.md`
  - manual/force ordinary no-send semantics documented.

## Validation

Focused Slice 1 matrix:

```text
PYTHONPYCACHEPREFIX=/tmp/om-pycache python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_daily_decision_brief_notification_flow.py \
  tests/test_daily_decision_brief_scenarios.py \
  tests/test_scheduled_notification_application.py \
  tests/test_multi_tick_domain_step2.py \
  tests/test_validate_config_notifications.py \
  tests/test_config_loader_validation_cache.py \
  tests/test_tick_run_context.py \
  tests/test_multi_account_tick.py \
  tests/test_runtime_trigger_context.py \
  tests/test_tick_cron.py \
  tests/test_phase3_audit_idempotency_hooks.py \
  tests/test_multi_tick_contract_batch2.py \
  tests/test_domain_engine_batch5.py \
  tests/test_cli_operator_commands.py \
  tests/test_pipeline_runtime_paths.py \
  tests/test_multi_tick_finalization_application.py \
  tests/test_tick_notification_perception_flow.py
```

Result: `209 passed`.

Additional checks:

- changed-file Ruff: `pass`;
- `python3.12 -m compileall -q src/application domain/domain`: `pass`;
- `git diff --check`: `pass`.

Key regression evidence:

- same-minute scheduled/manual/force keys differ while same-trigger retries remain stable;
- manual path rejects Daily Brief preparation and route resolution;
- multi-market fails before assembler/repository calls, writes terminal error metrics, and returns `2`;
- duplicate terminal multi-market failure returns `2` without rerunning guards/pipeline;
- empty, Compact, and malicious Legacy `AccountResult.notification_text` inputs produce the same scheduled message and delivery key;
- unchanged config with old `v1` cache is revalidated and rewritten as `notification-renderer-v2`.

## Docs decision

Updated current operator docs and CLI help because direct/manual `run tick` and `--force` now have a public ordinary-delivery safety contract. Historical Gateflow/review artifacts were not rewritten.

## Residual risks

| Risk | Classification | Destination |
|---|---|---|
| Public runtime-status/analysis still uses old notification artifact naming and health semantics | covered by later approved slice | Slice 2 |
| OpenD and delivery-failure notices still have separate renderers | covered by later approved slice | Slice 3 |
| Trade and maintenance receipts still have separate shells | covered by later approved slice | Slice 4 |
| Live scheduled launcher, version-skew deployment, and production warning evidence are not collected locally | assigned to later approved gate requiring authorization | Slice 5 compatibility release/observation |
| Legacy physical deletion and strict old-key rejection are intentionally not implemented | assigned to later work unit/hard pause | Slice 6 after compatibility evidence and CEO approval |

No unclassified residual risk remains for Slice 1 implementation.
