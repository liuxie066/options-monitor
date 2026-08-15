# Gateflow Plan — Sell Put Top1 HK Terminal Fee Contract

- Gate: `plan`
- Work unit: `sell-put-top1-hk-terminal-fee-contract`
- Branch: `feat/sell-put-top1-hk-terminal-fee-contract`
- Base: `origin/main@8528de6b59f89b815c9b481a69bfa6055333b93a`
- Artifact path: `docs/gateflow/sell-put-top1-hk-terminal-fee-contract/plan.md`
- Current gate: `plan`
- Next entry point: `plan review`

## Goal and completion signal

Provide the smallest source-level fee contract that lets later Top1 economics distinguish a complete HK terminal fee from an audit-only estimate. Existing lifecycle and assignment projections must stop emitting net economics when the terminal fee is incomplete.

The work unit passes when the pure calculator, both existing consumers, all fail-closed paths, adjacent regressions, and Kimi DeepReview pass. Runtime readiness remains `no-go` until a separately authorized real account fee-plan receipt exists.

## Non-goals

- No provider adapter, fee-plan lookup, registry, repository, configuration key, state machine, persistence, CLI, service, or generic fee framework.
- No default `lx` fee plan and no claim that a non-empty `fee_plan_ref` proves a real receipt.
- No W1B code, historical research, hidden validation, or production behavior outside the two existing fee consumers.
- No attempt to finish W0R's OpenD, quota, calendar, K-line, observation, or terms-capacity gaps.

## Direct code and source evidence

- `domain/domain/fee_calc.py::calc_futu_hk_stock_fee()` already owns the seven HK stock settlement components and is the correct arithmetic owner.
- `domain/domain/assigned_stock.py::_stock_fee_fact()` and `domain/domain/portfolio_assignment_scenario.py::_fee_fact()` currently consume that stock-only estimate for HK assignment, so they can treat incomplete terminal fees as complete.
- Futu's official HK fee schedule states that exercise adds HK$2 per contract, assignment does not charge that HK$2, and post-exercise commission/platform fees follow the account's HK stock fee package.
- Futu's exercise/settlement guidance states that an expired-worthless option is not exercised; therefore no exercise or stock-settlement leg occurs.

## Public source contract

Add the versioned pure entry point:

```python
FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION = "futu_hk_terminal_fee.v1"

calc_futu_hk_terminal_fee(
    kind,
    *,
    order_price=None,
    shares=0,
    contracts=0,
    account_fee_plan=None,
) -> dict
```

Supported kinds are exactly `assignment`, `exercise`, and `expired_worthless`; an unknown kind raises `ValueError`.

For assignment/exercise:

- `order_price` must be positive and finite.
- `shares` and `contracts` must be positive integral values; booleans, fractional values, non-finite values, and coercion by truncation are invalid.
- Invalid or missing economic inputs return `complete=false`, `basis=missing`, `amount=None`, `reason=stock_fee_inputs_incomplete`.
- The account plan requires a strict boolean `commission_free`, finite non-negative `platform_fee`, and non-empty `fee_plan_ref`.
- Missing or invalid plan facts return `complete=false`, `basis=missing`, `amount=None`, `reason=hk_account_fee_plan_missing`.
- When economic inputs are valid, `estimated_amount` may retain the clearly labelled standard-fixed, non-commission-free estimate for audit. Consumers must never substitute it for `amount`.
- Assignment option-leg fee is zero; exercise adds exactly HK$2 per contract.
- A complete plan-bound result is still `basis=estimated`; actual broker facts remain more authoritative.

Every result has exactly these keys:

```text
kind, currency, source, schedule_version,
complete, basis, amount, reason,
fee_plan_ref, missing_plan_facts,
components, estimated_components, estimated_amount, estimated_basis
```

All amounts are HKD floats rounded to six decimal places. `components` and `estimated_components` use the same component names and include `exercise_fee`; therefore a complete result obeys `amount == round(sum(components.values()), 6)`, while every result with a non-null audit estimate obeys `estimated_amount == round(sum(estimated_components.values()), 6)`. A missing result has `amount=None` and empty `components`. `fee_plan_ref` is always present and nullable. No dataclass, schema framework, or compatibility alias is added.

For expired-worthless:

- Return a complete zero terminal fee without requiring account-plan or settlement inputs because no exercise or stock settlement occurs.
- Include official source, schedule version, and stable reason `hk_expired_worthless_no_fee`.

The implementation extracts the existing HK stock component arithmetic into one private helper reused by both `calc_futu_hk_stock_fee()` and the terminal entry point. It does not add a class, registry, or second calculation path.

## Consumer behavior

### Assigned-stock lifecycle

- Preserve actual fee evidence before any estimate.
- HK assignment passes the original, uncoerced assigned shares/price and the ledger allocation's assigned-contract count to the terminal calculator. The fee path must not reuse lossy `int(float(...))` parsing.
- Current event/raw-payload mappings are not an authority for account fee plans. Until a separate validated receipt intake exists, this consumer calls the calculator without a plan binding and therefore remains missing unless actual broker fee evidence exists.
- HK expired-worthless option close uses the explicit zero-fee terminal result.
- A missing terminal fee remains `basis=missing`; the audit estimate may appear only as `estimated_amount`.
- If any lifecycle fee component is missing, `lifecycle_pnl_net` and `annualized_capital_efficiency` are `None`.

### Portfolio assignment scenario

- HK assignment uses the same terminal calculator and propagates schedule/ref provenance.
- Ordinary option-position mappings are not a validated fee-plan receipt. This consumer does not accept `account_fee_plan` from them in this slice.
- Missing plan binding leaves fee totals, net cash, and net asset distribution incomplete while retaining the audit-only estimate fields.
- Existing US fail-closed behavior remains unchanged.

## Affected files

### Production

- `domain/domain/fee_calc.py`
- `domain/domain/assigned_stock.py`
- `domain/domain/portfolio_assignment_scenario.py`

### Tests

- `tests/test_fee_calc.py`
- `tests/test_assigned_stock_projection.py`
- `tests/test_portfolio_assignment_scenario.py`

### Evidence and Gateflow

- `docs/performance/sell-put-top1-capability-preflight-20260814.md`
- `docs/gateflow/sell-put-top1-hk-terminal-fee-contract/`
- timestamped PlanReview and Kimi DeepReview artifacts

No other file is allowed without a new Gateflow scope decision.

## Implementation slice

### HKF-S1 — versioned terminal fee and fail-closed consumers

- Prerequisite: accepted goal confirmation and PlanReview pass.
- Implement strict numeric validation at the new calculator boundary without changing unrelated legacy calculator APIs.
- Preserve raw numeric inputs through the assigned-stock fee call so the strict boundary sees booleans, fractions, and non-finite values before any coercion.
- Reuse the existing stock arithmetic once; integrate only the two current incorrect consumers.
- Correct the preflight conclusion to distinguish source-contract lock from real account/runtime readiness.
- Stop if implementation needs a provider read, new persisted contract, configuration change, or modification outside the allowed files.

## Validation

```bash
./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_fee_calc.py \
  tests/test_assigned_stock_projection.py \
  tests/test_portfolio_assignment_scenario.py

./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_performance_assignment.py \
  tests/test_performance_engine.py \
  tests/test_option_positions_cli.py \
  tests/test_assigned_stock_sale_intake.py \
  tests/test_ledger_economics.py \
  tests/test_close_advice_runner.py \
  tests/test_close_advice_runner_gateway_reuse.py \
  tests/test_candidate_engine_parity.py \
  tests/test_candidate_engine_contract.py \
  tests/test_candidate_engine_phase2_contract.py \
  tests/test_portfolio_assignment_application.py \
  tests/test_portfolio_assignment_cli.py \
  tests/test_portfolio_agent_tool.py \
  tests/test_agent_plugin_contract.py \
  tests/test_strategy_lab.py \
  tests/test_shadow_replay.py \
  tests/test_shadow_replay_candidate_impact.py

./.venv/bin/ruff check \
  domain/domain/fee_calc.py \
  domain/domain/assigned_stock.py \
  domain/domain/portfolio_assignment_scenario.py \
  tests/test_fee_calc.py \
  tests/test_assigned_stock_projection.py \
  tests/test_portfolio_assignment_scenario.py

./.venv/bin/python \
  scripts/generate_dependency_graph.py --check
git diff --check
```

Money-path assertions must include hand-computed assignment/exercise values, commission-free and fixed-package variants, exact result keys and component-sum invariants, invalid/non-finite/fractional/bool numeric inputs, missing plan facts, actual-fee precedence, audit-estimate non-use, expired-worthless zero, ordinary payload plan-injection rejection, and both consumer fail-closed outputs.

After focused and adjacent validation, run the full repository suite before final review. Do not install a missing tool or invoke a provider to make a check green.

## Review and exit gates

1. PlanReview passes with no unresolved accepted finding.
2. Accepted plan is committed before implementation corrections.
3. Focused, adjacent, full-repository, Ruff, dependency, and patch checks pass, with sandbox-only limitations recorded separately.
4. Kimi DeepReview runs on this module, every accepted finding is fixed, and Kimi re-review reports no unresolved finding.
5. Aggregate/draft-PR Gateflow checks pass before handoff to W1B.

## Residual risks and owners

- Real `lx` account fee-plan receipt: separate W0R/provider remediation; still blocks provider-dependent research, hidden validation, and a real pilot.
- Validated fee-plan intake: the later W0R owner may call the pure calculator with a receipt-bound mapping; current event/position dictionaries cannot unlock completeness.
- End-to-end exercise event ingestion: later lifecycle/provider work; the pure formula is locked here.
- US terminal fees: later market-expansion work.
- Fee schedule changes: require a new schedule version and new evidence, not mutation of v1 history.

## Why this is the minimum

One new pure function reuses one existing formula and fixes the two consumers that currently produce misleading net economics. No new dependency, service, store, abstraction layer, or runtime lookup is added.
