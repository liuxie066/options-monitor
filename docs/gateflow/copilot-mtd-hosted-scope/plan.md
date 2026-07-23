# Gateflow Plan — Hosted MTD All-scope Normalization

- Work unit: `copilot-mtd-hosted-scope`
- Gate: `plan`
- Status: proposed
- Base: `origin/main@119a4271`

## Goal and Success Signal

Normalize the three all-scope markers observed in the v1.4.15 production Copilot trace so the
first successful `option_performance_report(period=mtd)` call omits account and broker and reads
the actual aggregate report. Preserve every existing fail-closed and accounting contract.

Completion requires focused and broad tests plus a post-deployment production P1 run where:

- the first recorded successful tool input contains `period=mtd` and no `account`/`broker`;
- its result scope contains the real configured accounts rather than a marker;
- the MTD response names all-account scope, realized PnL, cash, and assignment;
- no Feishu message or financial write occurs.

## First-principles Judgment

The model's marker choice is external protocol variability, but interpreting optional scope belongs
at the existing Copilot-specific normalizer, before tool execution. Fixing the report engine would
pollute accounting semantics; adding prompt templates alone would leave the observed values
unhandled; adding a global sentinel layer would broaden behavior without evidence.

## Affected Files

- `src/application/agent_tools/positions.py`
- `tests/test_copilot_phase1.py`
- Gateflow/review artifacts for this work unit

No public CLI, external agent-tool schema, storage schema, config, or docs contract changes.

## Contract Decisions

- Define a private, option-performance-Copilot-only set containing exactly `all`, `:all`, and
  `__omit__`.
- Compare markers after existing string trimming and case normalization.
- Remove matching optional `account`/`broker` keys; do not replace them with null.
- Keep empty strings invalid.
- Keep non-marker values byte-for-byte except for the existing outer trim.
- Add Copilot schema descriptions making omission/all-scope behavior explicit; this affects model
  guidance only and does not change the public tool schema.

## Implementation Slice S1

### Objective

Make hosted all-scope arguments reach the canonical report as omitted scope.

### Allowed Changes

- Add the private marker constant and narrow normalization branch in
  `_normalize_option_performance_copilot_input()`.
- Improve only the Copilot `account`/`broker` property descriptions.
- Add parameterized regression coverage through `copilot_tools.build_tool_payload()`.

### Invariants and Error Handling

- Empty optional scope remains `ValueError`.
- A real `lx`, `sy`, or broker value remains present.
- Period-field cleanup runs after scope normalization exactly as today.
- Channel-fixed `config_key` continues to override model input through the existing payload builder.

### Tests

Run:

```text
./.venv/bin/python -m pytest tests/test_copilot_phase1.py tests/test_copilot_p1_eval.py tests/test_option_performance_agent_tool.py
./.venv/bin/python -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
```

Assertions:

- each observed marker disappears for both optional fields;
- real scope values survive;
- empty scope remains rejected;
- the existing MTD/period and plugin contracts pass.

### Stop Condition

Stop if a marker is also a valid configured account or broker in current production facts, or if
normalization would need to move into the accounting/report engine.

## Review and Release Validation

- Run DeepReview on the slice and aggregate diff.
- Run full release checks because the fix changes the production Agent tool boundary.
- Publish a patch version only after all local gates pass.
- Upgrade `liuxie-incus`, run `om update verify --no-check-latest`, service checks, the direct MTD
  read, and the production P1 evaluator.
- If the hosted MTD case still fails, report the new trace as a blocking validation failure rather
  than broadening normalization speculatively.

## Risks

- Over-accepting aliases could hide a typo. Mitigation: exact three-value allowlist, Copilot-only.
- Model wording can still drift after correct facts arrive. Mitigation: production P1 gate remains
  mandatory.

## Why This Is Not Overengineered

The plan changes one existing normalizer and one focused test area. It adds no new layer, public
entity, workflow, config key, persistence, or accounting branch.

## Completion Report

Report the normalized contract, tests/reviews, patch release/commit, remote active version,
service health, direct MTD scope, production P1 result, and any remaining model-quality risk.

