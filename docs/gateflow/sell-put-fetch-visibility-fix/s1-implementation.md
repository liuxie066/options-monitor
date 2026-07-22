# Gateflow Implementation — S1 Fetch Visibility Authority

- Gate: `implementation`
- Work unit: `sell-put-fetch-visibility-fix`
- Slice: `S1`
- Scope: remove account-cash-derived Sell Put fetch-window mutation; preserve the existing Sell Call holdings prefilter.
- Production files: `src/application/prefilters.py`, `src/application/symbol_monitoring.py`, deleted `src/application/pipeline_steps.py`.
- Test files: `tests/test_prefilters_cash_limits.py`, `tests/test_required_data_fetch_planning.py`, `tests/run_smoke.py`.

## Decision

Sell Put required-data visibility is now derived only from symbol market/config and spot. `apply_prefilters()` no longer reads account cash to change `sell_put.max_strike` or disable Put scanning. The obsolete native-currency cash-cap helper and its FX-only call arguments were removed. Final account cash eligibility remains owned by the existing canonical post-fetch enrichment path.

## Regression evidence

- Red before implementation: the new account-invariance tests failed because `apply_prefilters()` still required the FX arguments and retained the cash-dependent branch (`7 failed, 1 passed`).
- Focused green: `python3.12 -m pytest -q -p no:cacheprovider tests/test_prefilters_cash_limits.py tests/test_required_data_fetch_planning.py tests/test_symbol_monitoring_fetch_spec_merge.py` -> `35 passed`.
- Smoke green: `python3.12 tests/run_smoke.py` -> `OK (smoke)`.
- Deepreview: `docs/reviews/code-review-20260722-235801.md` -> `未发现实质性问题`.
- The production-shaped TCOM planner regression resolves the same Put plan for lx and sy: expirations `2026-08-21` and `2026-09-18`, strike window `34.456..43.07` at spot `43.07`.
- Missing spot retains the configured fallback window `36.0..45.0`.

## Residual risk

- This slice does not yet prove the final cash gate accepts affordable TCOM rows through the total-CNY fallback; that is S2.
- Shared required-data order invariance and concurrent plan construction remain S2.
- No production canary or external write was performed.

- Status: `accepted`.
