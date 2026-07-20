# Combo Yield Agent Config — Implementation Plan

- Status: accepted
- Date: 2026-07-20
- Work unit: add preview/confirm configuration support for `combo_yield.enabled`, then release and enable it for `3690.HK` in production.

## Goal and success signals

1. IM/Assistant accepts `combo_yield.enabled=true|false` in the existing symbol-edit grammar.
2. Preview remains read-only; apply still requires the existing pending-operation confirmation gate.
3. YAML authoring writes only `markets.<market>.overrides.<symbol>.combo_yield.enabled`, validates all markets, backs up `config.yaml`, and rebuilds runtime snapshots on apply.
4. Existing Covered Call and Sell Put behavior remains unchanged.
5. Release checks pass; production is upgraded; `3690.HK` resolves with Combo Yield enabled through a read-only config surface.

## Non-goals

- Generic arbitrary-path config editing.
- New Combo Yield thresholds or strategy policy fields.
- Triggering a scan or sending a notification.
- Changing Combo Yield ranking/filter/runtime semantics.

## Evidence and ownership

- `src/application/assistant/symbol_operations.py` owns IM symbol-edit allowlisting and preview/confirm payload construction.
- `src/application/config_yaml_symbols.py` owns canonical YAML mutation, validation, backup, and runtime rebuild.
- `tests/test_config_yaml.py` covers low-level YAML mutation.
- `tests/test_inbound_control.py` covers the user-facing preview/confirm state machine.
- CLI `config symbol set` shares the low-level writer and should expose the same field for operator parity.

## Implementation slice

1. Add optional `combo_yield_enabled` to `set_yaml_symbol_config()` and `_mutate_symbol_config()`.
2. Mutate only `combo_yield.enabled`; do not alter `use`, Sell Put, or Covered Call templates because Combo Yield is independently gated.
3. Add `--combo-yield-enabled` to the existing CLI facade and forward it.
4. Add `combo_yield.enabled` to the Assistant YAML allowlist, payload conversion, forwarding, and clarification text.
5. Add focused tests for dry-run mutation, Assistant preview/confirmation/rebuild, and CLI forwarding.
6. Run focused tests, config validation/build dry-runs, dependency graph check, and release checks.

## Rollout and rollback

1. Publish a patch release because this is a backward-compatible capability addition with no schema migration.
2. Upgrade `liuxie-incus` using the existing `om update` flow.
3. Preview the remote YAML symbol edit, apply with backup/rebuild, and verify through read-only config/status tools.
4. Roll back by restoring the generated YAML backup and rebuilding runtime snapshots; package rollback remains the existing release rollback path.

## Residual risks

- Natural-language model behavior may phrase the operation differently; the deterministic field-path form remains the contract.
- Enabling the strategy does not guarantee a candidate passes downstream filters.
