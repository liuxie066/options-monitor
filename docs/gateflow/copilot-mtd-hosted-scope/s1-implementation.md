# Gateflow S1 Implementation

- Work unit: `copilot-mtd-hosted-scope`
- Slice: S1 — hosted all-scope normalization
- Gate: `implementation`
- Status: complete

## Changes

- Added an option-performance-Copilot-only allowlist for the production-observed markers `all`,
  `:all`, and `__omit__`.
- `_normalize_option_performance_copilot_input()` now removes those values from optional account
  and broker scope before safe defaults and channel-fixed input are applied.
- Empty strings still raise `ValueError`; real account/broker values remain present.
- Copilot schema descriptions now tell the model to omit the field or use `all` for aggregate
  scope.
- Added parameterized regression coverage for every observed marker, uppercase/whitespace
  handling, fixed market scope, real scope passthrough, and the existing invalid-empty path.

## Changed Files

- `src/application/agent_tools/positions.py`
- `tests/test_copilot_phase1.py`

## Validation

```text
92 passed:
tests/test_copilot_phase1.py
tests/test_copilot_p1_eval.py
tests/test_option_performance_agent_tool.py

102 passed, 1 existing deprecation warning:
tests/test_agent_plugin_contract.py
tests/test_agent_plugin_smoke.py
```

The first attempted test command used the original checkout's lightweight `.venv`, which has no
pytest and therefore collected no tests. The supported pyenv Python 3.12.13 environment was then
used for both passing commands.

## Scope and Safety

- No report engine, accounting, ledger, storage, config, transport, or sending code changed.
- No production state or Feishu message was written.
- The original dirty workspace remains untouched.

## Residual Risks

- Hosted-model wording after receiving correct facts still requires production P1 validation.
  Classification: mandatory post-release validation for this work unit.
- Unknown future scope aliases are intentionally unsupported until production evidence exists.
  Classification: future model-evaluation maintenance, not current scope.

## Next Entry Point

`code review`
