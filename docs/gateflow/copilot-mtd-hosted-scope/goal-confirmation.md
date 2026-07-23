# Gateflow Goal Confirmation

- Work unit: `copilot-mtd-hosted-scope`
- Gate: `goal confirmation`
- Status: confirmed
- Base: `origin/main@119a4271`
- Branch: `fix/copilot-mtd-hosted-scope`

## Confirmed Goal

Make the production Copilot treat the hosted model's explicit all-scope markers as omission for
`option_performance_report.account` and `.broker`, so the exact question
`7月 mtd 的期权收益` reads the real all-account MTD report instead of an empty synthetic account.

The user's prior Gateflow confirmation covers this same MTD answer-quality goal. The separately
authorized release/deployment verification exposed the concrete hosted-model failure, so no new
product or accounting decision is required.

## Motivation and Direct Evidence

The v1.4.15 production P1 report showed:

- the first malformed empty-account call failed closed as designed;
- subsequent calls used `__omit__`, `:all`, and `all` for both account and broker;
- those literal values reached the option-performance engine and produced empty scopes such as
  `accounts=["__omit__"]`;
- the model then answered that no concrete financial result was available even though a direct
  all-account `period=mtd` tool call returned the real `lx` and `sy` facts.

The owning boundary is
`src/application/agent_tools/positions.py::_normalize_option_performance_copilot_input()`.
`src/application/copilot/tools.py::build_tool_payload()` already delegates Copilot-specific input
normalization there before applying safe defaults.

## Success Signals

- Copilot-only values `all`, `:all`, and `__omit__` are removed from optional `account` and
  `broker` fields before execution.
- The normalized payload still contains `period=mtd`, the channel-fixed `config_key`, and existing
  safe defaults.
- Real account/broker filters remain unchanged.
- Empty strings and invalid period combinations remain fail closed.
- Focused regression, full release checks, and the production P1 MTD case pass after deployment.

## Scope Boundary and Non-goals

- No ledger, PnL, cash, assignment, FX, or fee semantics change.
- No generic sentinel framework and no normalization changes for unrelated tools.
- No production configuration, model selection, Feishu transport, or notification behavior change.
- No acceptance of empty strings, nulls, arbitrary unknown account names, or arbitrary aliases.
- No live Feishu message is sent during validation.

## Open Questions

None. The production trace identifies the exact values and owning boundary.

## Residual Risk Classification

- Hosted wording may still drift after correct data reaches the model. This remains covered by the
  existing deterministic P1 evaluator and must be re-run against production after deployment.

