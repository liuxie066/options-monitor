# Gateflow Plan — Sell Put account-invariant fetch visibility

## Gate state

- Work unit: `sell-put-fetch-visibility-fix`
- Current gate: accepted plan
- Next gate: implementation S1
- Status: `pass-with-risks` after `docs/reviews/plan-review-20260722-233358.md`; implementation has not started
- Artifact path: `docs/gateflow/sell-put-fetch-visibility-fix/plan.md`

## Goal and motivation

Fix the production defect where account cash composition changes the Sell Put market-data window before candidates exist. In run `20260722T140036Z-add5f4`, TCOM used the same market configuration for `lx` and `sy`, but the early account-cash prefilter changed the resolved Put window to:

- `lx`: `17.41984..21.7748`, producing zero visible Put rows;
- `sy`: `34.456..43.07`, producing the 35P/40P candidates.

The fetch layer must answer “which configured market contracts are visible?” Account cash capacity must answer only “may this account recommend this already-visible contract?”. Those decisions must have one owner each.

## Success signals

1. For the same symbol config, spot and expirations, `lx` and `sy` generate the same Sell Put fetch plan regardless of native-currency cash, account order, or plan-construction concurrency.
2. With TCOM config `max_strike=45` and spot `43.07`, both accounts resolve the existing market window `34.456..43.07`; the plan must not assert a network window of `45` while a valid positive spot exists.
3. With spot unavailable, existing planner behavior remains intact and the resolved maximum falls back to configured `45`.
4. A production-shaped `lx` fixture carries TCOM 35P/40P through real cash enrichment and the canonical final capacity gate using `total_cny`; neither candidate receives a `cash_reserve` rejection.
5. Insufficient or unreliable capacity still fails closed with the existing trace reasons.
6. Sell Call holding prefiltering and Combo Yield market-scope independence remain unchanged.
7. Full-watchlist request volume and duration meet the quantitative rollout budget defined below.

## Non-goals and scope boundary

- Do not change `compute_sell_put_cash_capacity()` basis precedence (`base_cny` before `total_cny` before USD fallback).
- Do not change collateral policy, exchange-rate semantics, candidate ranking, underwriting, notification formatting, config keys, schemas, state formats, or public CLI/tool contracts.
- Do not add locks or redesign shared required-data persistence in this work unit. Current production has no `multi_account_max_workers`/`account_max_workers` override and therefore uses one account worker. If equal fetch specs still expose a shared-write race, create a separate concurrency/recovery bug.
- Do not remove configured/spot/DTE market bounds or fetch the unbounded option chain.
- Do not send notifications, modify production config, write positions/trades, or deploy without explicit authorization.
- Do not touch the unrelated Close Advice changes currently present in the worktree. Implementation must use a clean worktree/branch based on the intended base ref.

## First-principles judgment and direct evidence

### Owning boundaries

- `src/application/required_data_planning.py` owns the market fetch window. `_resolve_put_side_plan()` already resolves `max_strike = min(configured_max_strike, spot_reference)` when spot is positive.
- `src/application/sell_put_cash.py` owns cash enrichment, including conversion of every known cash currency to CNY and subtraction of total secured cash.
- `domain/domain/risk_capacity.py` owns the final fail-closed eligibility decision.
- `src/application/prefilters.py` currently violates those boundaries by deriving a second, native-currency-only Put strike cap before required-data planning.

### Production-shaped evidence

The read-only production snapshot for `lx` in run `20260722T140036Z-add5f4` contains:

- cash: HKD `666787.5`, USD `10177.48`;
- existing secured cash: HKD `386500`, USD `8000`;
- total secured CNY: `388092.51161900006`;
- USDCNY: `6.7711`; HKDCNY: `0.863968206`;
- free total capacity: approximately CNY `256903.42`;
- TCOM 35P/40P one-contract requirements: approximately CNY `23698.85` and `27084.40`.

Therefore those contracts are supported by the existing `total_cny` policy once they become visible. No collateral-policy change is required for this observed defect.

## Contract and state decisions

### Prefilter contract

For Sell Put:

```text
want_put_out == want_put_in
sp_out == sp_in by value
portfolio_ctx and FX inputs cannot change either result
```

For Sell Call, current account-holding behavior remains unchanged.

### Fetch-plan contract

For TCOM with `sell_put.max_strike=45`:

```text
spot = 43.07  -> resolved Put window = 34.456..43.07
spot = None   -> resolved Put maximum = 45
```

The exact lower fallback follows the existing `DEFAULT_FETCH_NEAR_BOUND_EXPAND_PCT` behavior. Tests assert the resolved `OptionSideFetchPlan`, not the legacy `ensure_required_data(max_strike=...)` compatibility argument.

### Capacity contract

The implementation does not change capacity policy. Tests must call the real enrichment/filter boundary and then evaluate the enriched row with `compute_sell_put_cash_capacity()`:

- production-shaped `lx`: accepted with `basis="total_cny"`;
- known insufficient total: rejected with `total_cny_cash_insufficient`;
- missing/unreliable secured-cash or FX basis: rejected fail-closed with the existing explicit reason.

### Public/schema/state impact

- Public CLI/tool contract: none.
- Config/schema change: none.
- Persistent state or migration: none.
- Rollback: code-only rollback; no data reversal is required.

## Implementation decisions

1. Remove the Sell Put cash-cap branch from `apply_prefilters()` rather than reimplementing the final CNY capacity policy in the fetch layer.
2. Keep Sell Call holding prefiltering in `apply_prefilters()`.
3. Remove the two FX arguments from the internal `apply_prefilters()` signature and its single production caller; retain FX fields on `SymbolMonitoringInputs` because they still build the downstream `CurrencyConverter`.
4. Delete `derive_put_max_strike_from_cash()` and `src/application/pipeline_steps.py` after an `rg` call-site check proves there are no remaining runtime consumers. Replace obsolete helper/smoke tests with behavioral regressions.
5. Do not alter required-data planner code. The regression must preserve its configured/spot/DTE behavior.
6. Do not alter final risk-capacity code unless the production-shaped test disproves the recorded evidence. Such a failure is a stop condition requiring renewed scope review, not permission to change collateral policy.

This is the smallest solution because it deletes a duplicated policy decision and relies on existing owners. It adds no new abstraction, config, state, migration, lock, or public surface.

## Affected files/modules

Expected production changes:

- `src/application/prefilters.py`
- `src/application/symbol_monitoring.py`
- `src/application/pipeline_steps.py` — delete only after the call-site check

Expected tests:

- `tests/test_prefilters_cash_limits.py`
- `tests/test_symbol_monitoring_fetch_spec_merge.py`
- `tests/test_required_data_fetch_planning.py`
- `tests/test_pipeline_fetch_read_model_boundary.py`
- `tests/test_sell_put_cash_total_cny.py`
- `tests/test_candidate_filter_trace.py`
- `tests/run_smoke.py`

No other module is allowed unless implementation reveals a directly failing import/call contract; that is a stop-and-record condition.

## Implementation slices

### S1 — Remove the duplicate Put cash authority

- **Objective**: make `apply_prefilters()` account-invariant for Sell Put while preserving Sell Call behavior.
- **Allowed production files**: `src/application/prefilters.py`, `src/application/symbol_monitoring.py`, `src/application/pipeline_steps.py`.
- **Allowed test files**: `tests/test_prefilters_cash_limits.py`, `tests/test_symbol_monitoring_fetch_spec_merge.py`, `tests/test_required_data_fetch_planning.py`, `tests/run_smoke.py`.
- **Prerequisite**: clean worktree/branch for this work unit; `rg` confirms helper call sites.
- **Exact changes**:
  - add failing prefilter regressions for the production-shaped `lx` context, `sy` with no USD cash, zero native cash, and missing portfolio context;
  - each asserts `want_put=True` and an unchanged Sell Put config;
  - add the real planner regression with fixed spot `43.07` and fixed expirations; assert both account contexts resolve `34.456..43.07` and identical side-plan debug payloads;
  - assert the no-spot fallback remains `max_strike=45`;
  - remove the Put cash-cap branch/import/helper and obsolete tests;
  - remove internal prefilter FX call arguments without changing converter construction.
- **Non-goals**: no changes to planner, capacity, fetch persistence, ranking, or notifications.
- **Completion signal**: the new tests fail on the old code for the account-cash mutation and pass after removal; existing Yield Enhancement and Sell Call prefilter tests pass.
- **Stop conditions**: any external runtime caller of `derive_put_max_strike_from_cash()`, any public dependency on the exact prefilter signature, or any need to alter planner semantics.

### S2 — Prove final cash behavior and shared-run invariance

- **Objective**: prove the production outcome rather than only the intermediate plan.
- **Allowed production files**: none unless S1 exposed a directly failing contract and the scope is re-reviewed.
- **Allowed test files**: `tests/test_pipeline_fetch_read_model_boundary.py`, `tests/test_sell_put_cash_total_cny.py`, `tests/test_candidate_filter_trace.py`, `tests/test_symbol_monitoring_fetch_spec_merge.py`.
- **Prerequisite**: S1 tests pass.
- **Exact changes**:
  - create a sanitized `lx` portfolio fixture using the recorded cash, secured totals and exchange rates; construct the converter with `usd_per_cny = 1 / 6.7711` and `cny_per_hkd = 0.863968206`;
  - pass TCOM 35P/40P rows with multiplier `100` and USD currency through real `_enrich_and_filter_sell_put_cash()`;
  - assert both rows remain, the enriched fields produce accepted `total_cny` capacities, and no `cash_reserve` trace exists for them;
  - add insufficient-total and missing-basis counterparts that remain fail-closed and emit existing trace rules;
  - parameterize shared-run order as `lx→sy` and `sy→lx` using the same temporary required-data directory, actual fetch-plan/coverage code, fixed trading date `2026-07-22`, spot `43.07`, expirations `2026-08-21` and `2026-09-18`, and a deterministic OpenD payload containing Put strikes `35`, `40` and `42.5` for each expiration;
  - disable Sell Call and Combo Yield in that shared-order fixture so the assertion isolates the Sell Put market contract; account-specific stock holdings must not be mistaken for a Put-scope regression;
  - include spot `43.07` and the required realized-volatility field on every deterministic payload row so a second fetch cannot be caused by an intentionally incomplete fixture;
  - assert both accounts create identical Put specs, the second account consumes coverage without an account-specific refetch, and the final visible Put universe is identical in both orders;
  - run the plan-construction portion through `run_account_outcomes(..., max_workers=2)` and assert both account contexts produce the identical resolved spec. Do not use this test to claim cross-process write atomicity.
- **Non-goals**: no new locking and no change to current production worker count.
- **Completion signal**: deterministic TCOM 35P/40P cash acceptance is proven end to end, both sequential orders are equal, and concurrent plan construction is account-invariant.
- **Stop conditions**: production-shaped fixture selects `base_cny` or fails final capacity unexpectedly; shared order still changes coverage despite identical specs; either requires root-cause reassessment before release.

### S3 — Quality and rollout budget evidence

- **Objective**: demonstrate that removing the cap does not exceed the operational fetch budget.
- **Allowed production files**: none.
- **Allowed artifacts**: the implementation/fix artifact for this work unit and local test output; remote canary only after explicit authorization and release.
- **Prerequisite**: S1 and S2 pass.
- **Exact validation**:
  - use cached option-chain fixtures or read-only run artifacts to calculate full US watchlist before/after `snapshot_requested_codes` and expected snapshot batch calls;
  - capture comparable US scheduled-run baselines from the latest seven successful runs: `pipeline_ms`, `market_snapshot_opend_calls`, and rate-gate wait;
  - define canary pass as: no rate-limit/truncation/fetch error, duration below 600 seconds, and duration no greater than `max(baseline_p95 * 1.25, baseline_p95 + 30 seconds)`; any larger regression blocks release pending an account-independent market-window optimization;
  - expiration selection must remain unchanged; option-chain calls must not increase for the same run-level market plan, while a decrease from removing account-specific refetch is allowed and must be reported; only snapshot code/batch counts may increase according to the resolved window.
- **Non-goals**: no optimization implementation in this slice.
- **Completion signal**: budget evidence is recorded with measured numbers and a pass/fail decision.
- **Stop conditions**: missing comparable baseline, budget violation, or evidence that option-chain/expiration calls changed.

## Test and validation commands

Focused gate:

```bash
./.venv/bin/python -m pytest \
  tests/test_prefilters_cash_limits.py \
  tests/test_symbol_monitoring_fetch_spec_merge.py \
  tests/test_required_data_fetch_planning.py \
  tests/test_pipeline_fetch_read_model_boundary.py \
  tests/test_sell_put_cash_total_cny.py \
  tests/test_candidate_filter_trace.py
```

Tick/regression gate:

```bash
./.venv/bin/python -m pytest \
  tests/test_required_data_prefetch_inprocess.py \
  tests/test_multi_account_tick.py \
  tests/test_multi_tick_*.py \
  tests/test_unified_tick_entrypoint.py
```

Repository gate:

```bash
./.venv/bin/python -m pytest -q
git diff --check
```

Expected assertions are the contracts in S1/S2; a green suite without those exact assertions is insufficient.

## Rollout and production verification

1. Release/deployment is outside the local implementation mutation and requires the normal VERSION-driven release flow plus explicit operator authorization.
2. First canary: one isolated US run with `--accounts lx sy --symbols TCOM --no-send`. It may write isolated run artifacts and call market data, so obtain authorization first.
3. Verify both account artifacts record the same resolved Put window `34.456..43.07` for the observed spot fixture or the same market-derived window for the live spot.
4. Verify TCOM Put contracts enter both candidate pipelines. Live market filters may reject a contract, but every rejection must be explicit; deterministic local fixtures, not changing live prices, prove 35P/40P cash acceptance.
5. Second canary: authorized full US watchlist `--no-send`; apply the S3 quantitative budget.
6. If correctness or budget fails, roll back the code release. No config/state rollback is necessary.

## Docs decision

- No public command/config documentation change is required because no public contract changes.
- Add a concise `CHANGELOG.md` entry only when packaging the release, describing the removal of account-cash-dependent Sell Put fetch visibility.
- Gateflow implementation/review/closeout artifacts must record the production-shaped evidence and rollout budget decision.

## Risks and classified residuals

- **Snapshot expansion**: fixed in current work unit by S3 measurement and rollout gate; optimization is not pre-authorized.
- **Cross-process shared-file atomicity**: assigned to a later concurrency/recovery work unit only if reproduced after specs are equal. Current production worker count is 1, and this plan covers both sequential account orders.
- **`base_cny` versus `total_cny` policy**: explicitly outside this work unit; requires CEO policy decision if a future account contains a low CNY key plus sufficient foreign-currency total.
- **Live market drift**: handled by separating deterministic fixture acceptance from live canary observability.
- **Dirty branch ownership**: implementation must use a clean branch/worktree and must not stage or modify the current Close Advice changes.

## Open questions

None blocking. Any stop condition above reopens plan review before production-code expansion.

## Completion report format

The implementation handoff must report:

1. changed files and exact deleted authority path;
2. deterministic before/after TCOM fetch-plan evidence for `lx` and `sy`;
3. production-shaped 35P/40P capacity results and reject-trace negatives;
4. shared-directory order and concurrent-plan invariance results;
5. focused, tick and full-suite command results;
6. measured request/duration budget and decision;
7. docs/release decision;
8. residual risks with owner/destination;
9. whether release/canary remains pending explicit authorization.
