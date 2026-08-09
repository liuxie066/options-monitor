# Gateflow Slice 1 Implementation — Copilot Input Reliability

- Work unit: `copilot-option-performance-mtd`
- Slice: `S1`
- Gate: `implementation`
- Date: 2026-07-23
- Status: accepted after deepreview fix and re-review
- Base plan commit: `f2ccfb55`
- Initial review: `docs/reviews/code-review-20260723-165433.md`
- Accepted re-review: `docs/reviews/code-review-20260723-165944.md`

## Implemented

### Tool-owned Copilot input normalization

- Added one optional `copilot_input_normalizer` callable beside the existing
  `copilot_input_schema` metadata on `AgentTool`.
- `build_tool_payload()` now:
  1. collects explicit static/model inputs;
  2. runs the tool-owned normalizer on model-controllable inputs;
  3. adds only non-null safe defaults;
  4. applies fixed UI/runtime scene inputs last so they cannot be pruned or overridden.
- Explicit `None` and empty-string model/static values are no longer silently discarded.
- Copilot schema/default publishing no longer emits safe `default:null`.

### Option performance period adapter

- Removed fake null defaults for paths, scope, and mutually exclusive period fields.
- Preserved the public Agent manifest's existing `period=mtd` safe default.
- Normalizes only when the explicit payload contains one valid period discriminator:
  - MTD/YTD keep `as_of_date`;
  - month keeps `month`;
  - year keeps `year`;
  - range keeps `start_date/end_date`.
- Unknown periods, missing discriminators, blank account/broker, and invalid relevant fields
  remain fail closed.
- A fixed UI month conflicting with model MTD remains in the final payload and fails closed;
  aligned `period=month` calls use the fixed month.

### Plan compatibility correction

The first broader contract run proved that `period=mtd` is a public Agent manifest contract
covered by `tests/test_agent_plugin_contract.py`. The implementation therefore preserves that
default and normalizes explicit inputs before applying it. The accepted plan artifacts were
updated to describe this compatibility-preserving sequence; no goal or scope changed.

## Regression evidence

Added tests proving:

- the Copilot description has no hidden path/null defaults;
- all five period kinds retain only their legal fields when explicitly selected;
- the exact online MTD payload with month/year/range pollution becomes one legal first call;
- `month` without explicit period remains ambiguous and fails;
- explicit null hidden path input remains visible and fails schema validation;
- blank account fails instead of widening to all accounts;
- fixed month scope survives model MTD normalization and remains auditable as a conflict;
- public ambiguous-period behavior and aggregate-account default are unchanged;
- public plugin manifest keeps `period=mtd`.

## Validation

```text
/Volumes/Workspace/workspace/options-monitor/.venv/bin/ruff check \
  src/application/agent_tools/base.py \
  src/application/agent_tools/positions.py \
  src/application/copilot/tools.py \
  src/application/copilot/engine.py \
  src/application/copilot/host.py \
  tests/test_copilot_phase1.py \
  tests/test_option_performance_agent_tool.py

All checks passed.
```

```text
PYTHONPYCACHEPREFIX=/tmp/om-copilot-option-performance-pycache \
python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_copilot_phase1.py \
  tests/test_option_performance_agent_tool.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py

175 passed, 1 pre-existing deprecation warning.
```

The first attempted test command used the original checkout's `.venv/bin/python`, which lacks
pytest; no test ran and no environment was modified. Validation then used the supported local
Python 3.12 with the existing pytest installation and an external bytecode cache.

## Safety

- No config, ledger, Feishu, notification, or production state was read or written.
- No files outside the isolated worktree were modified.
- No release or deployment action was performed.
