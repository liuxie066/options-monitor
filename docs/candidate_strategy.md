# Candidate Strategy Contract

This document defines the target Put/Call candidate filtering and ranking contract.
It is written for the current decision/execution split:

- Engine owns deterministic decisions, filtering semantics, ranking semantics, DTOs, and reject reasons.
- Scripts/infra own external effects: option-chain fetch, holdings read, option_positions read, OpenD/Futu calls, and notification delivery.
- Current scanner scripts still carry part of the candidate strategy. Future refactors should move those rules behind an Engine candidate decision boundary without changing outputs first.

## Scope

This contract applies to both Sell Put and Sell Call candidates.

Put/Call share the same stages, but use separate parameters and metrics:

- Put: cash-secured capacity, annualized net return on cash basis.
- Call: covered-share capacity, annualized net premium return, net income, if-exercised total return for ranking.

## Stage 0: Input Normalization

Normalize all upstream source data before strategy decisions:

- Contract identity: `symbol`, `option_type`, `expiration`, `dte`, `contract_symbol`.
- Price fields: `spot`, `strike`, `bid`, `ask`, `last_price`, `mid`.
- Execution-quality fields: `open_interest`, `volume`, `implied_volatility`, `delta`.
- Contract economics: `currency`, `multiplier`.
- Event context: earnings, ex-dividend, and future macro/event calendar fields when available.
- Account context:
  - Put: cash by currency, open short-put cash-secured usage, FX rates.
  - Call: shares, average cost, open short-call locked shares.

Reject candidates with missing critical fields and attach a reject reason.

Minimum critical fields:

- Common: `symbol`, `option_type`, `expiration`, `dte`, `spot`, `strike`, `mid`, `multiplier`.
- Put: put contract type and `strike < spot`.
- Call: call contract type, valid `avg_cost`, valid `shares`, and cover capacity inputs.

## Stage 1: Hard Constraints

Apply execution feasibility gates before return optimization:

- DTE range: `min_dte <= dte <= max_dte`.
- Strike range:
  - Absolute: `min_strike` / `max_strike`.
  - Relative to spot when configured.
- Account feasibility:
  - Put: cash-secured requirement must be within configured/account capacity.
  - Call: available covered shares must satisfy contract multiplier demand.

Put capacity rule:

- Compute required cash using strike, multiplier, and candidate currency.
- Prefer base-currency gating when normalized CNY fields are available.
- Fallback to native-currency gating only when base-currency fields are unavailable.

Call capacity rule:

- Compute available shares as `shares_total - shares_locked`.
- Compute available covered contracts as `available_shares // multiplier`.
- Reject if available covered contracts is less than one.

## Stage 2: Return Floor

Apply minimum return rules after the candidate is feasible:

- Annualized return must meet the configured threshold.
- Optional single-trade net income must meet the configured threshold.
- Net income must meet the configured threshold when enabled.

Current threshold resolution should remain:

- Symbol config overrides template config.
- Template config overrides default constants.
- Default annualized threshold is `0.07` unless explicitly changed in code.

## Stage 3: Risk And Execution Quality

Apply high-accident-risk and execution-quality gates:

- `min_open_interest`.
- `min_volume`.
- `max_spread_ratio`.
- Key event windows:
  - Earnings.
  - Ex-dividend.
  - Future macro/event calendar items when supported.

Current D3 contract:

- Global template D3 hard fields are limited to `min_open_interest`, `min_volume`, and `max_spread_ratio`.
- Symbol-level D3 fields are forbidden by config validation.
- D3 event mode defaults to `warn`; event hits are annotated with `D3_EVENT_WARN` and are not hard rejects by default.

## Stage 4: Ranking And Top Pick

Ranking must be deterministic and separate from hard filtering.

Candidate CSV, alerts, and summary top contract use the same simple ranking.

Put candidate ranking:

- Primary: annualized net return on cash basis.
- Secondary: net income.

Call candidate ranking:

- Primary: annualized net premium return.
- Secondary: net income.

Layered output:

- Risk layers are `激进`, `中性`, `保守`.
- Layering is an output diversification policy, not a replacement for hard filtering.
- When layered output is requested, pick one best candidate from each layer, then fill remaining slots by overall rank.

## Reject Reason Contract

Reject reasons should be machine-readable and stable enough for tests and reports.

Recommended reason groups:

- `input_missing`: required input missing or invalid.
- `hard_dte`: DTE outside configured range.
- `hard_strike`: strike outside configured range or violates moneyness rule.
- `hard_capacity_put`: put cash requirement exceeds allowed capacity.
- `hard_capacity_call`: call cover capacity is insufficient.
- `return_annualized`: annualized return below threshold.
- `return_net_income`: net income below threshold.
- `risk_open_interest`: open interest below threshold.
- `risk_volume`: volume below threshold.
- `risk_spread`: spread ratio above threshold.
- `risk_event_warn`: key event hit in warn mode.
- `risk_event_reject`: key event hit in reject mode, if enabled later.

## Legacy Reject Bridge

Current scanner reject logs use legacy rule names. The Engine bridge preserves those rows while adding stable Engine stage/reason fields.

Mapping:

- `min_annualized_return` -> `stage2_return_floor` / `return_annualized`.
- `min_net_income` -> `stage2_return_floor` / `return_net_income`.
- `max_spread_ratio` -> `stage3_risk_filter` / `risk_spread`.

Bridge behavior:

- Preserve original `reject_stage` as `legacy_reject_stage`.
- Preserve original `reject_rule` as `legacy_reject_rule`.
- Preserve contract identity fields when present: `symbol`, `contract_symbol`, `expiration`, `strike`, and `mode`.
- Raise on unknown legacy rules so new scanner rules cannot silently bypass the Engine reason contract.

Current scanner CSV compatibility:

- Keep existing reject log columns unchanged.
- Append only `engine_reject_stage` and `engine_reject_reason` for low-noise Engine integration.

## Current Implementation Map

- Input source schema: `docs/required_data_schema.md`.
- Candidate scanner logic: `scripts/scan_sell_put.py`, `scripts/scan_sell_call.py`.
- Engine strategy implementation: `domain/domain/engine/candidate_strategy.py`.
- Engine strategy pipeline helpers: `filter_rank_candidates_with_reject_log`, `rank_scored_candidates`.
- Shared simple ranking: `domain/domain/engine/candidate_engine.py`.
- Production scanner/render entrypoints import the Engine strategy directly.
- Compatibility wrapper for old callers: `scripts/option_candidate_strategy.py`.
- D3 event annotation: `scripts/d3_event_filter.py`.
- Config D3 validation: `scripts/validate_config.py`.
- Put cash enrichment and headroom gate: `scripts/sell_put_steps.py`, `scripts/sell_put_cash.py`.
- Call covered-capacity gate: `scripts/sell_call_steps.py`, `scripts/scan_sell_call.py`.
- Summary report assembly: `scripts/report_summaries.py`.
- Alert priority classification: `scripts/alert_engine.py`.

## Refactor Direction

The next safe refactor is to introduce an Engine candidate boundary without changing behavior:

- Add candidate DTOs for normalized inputs and decisions.
- Mirror current scanner filter behavior into pure Engine functions.
- Run old and new paths side by side in tests.
- Move scanner scripts toward thin adapters after output parity is locked.
