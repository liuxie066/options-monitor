# Gateflow Implementation — S2 Outcome and Shared-Run Evidence

- Gate: `implementation`
- Work unit: `sell-put-fetch-visibility-fix`
- Slice: `S2`
- Scope: test-only proof of final cash behavior, shared required-data order invariance, and concurrent plan construction.
- Production files: none.
- Test files: `tests/test_candidate_filter_trace.py`, `tests/test_pipeline_fetch_read_model_boundary.py`.

## Production-shaped cash result

The sanitized lx fixture uses cash HKD `666787.5` / USD `10177.48`, secured cash HKD `386500` / USD `8000`, secured total CNY `388092.51161900006`, USDCNY `6.7711`, and HKDCNY `0.863968206`.

TCOM 35P and 40P rows pass through the real `_enrich_and_filter_sell_put_cash()` boundary:

- 35P required CNY: `23698.85`; accepted with `basis="total_cny"`.
- 40P required CNY: `27084.40`; accepted with `basis="total_cny"`.
- Both rows remain in the returned frame and no `cash_reserve` trace file is created.
- An insufficient total-CNY fixture is rejected with `total_cny_cash_insufficient`.
- A missing portfolio/cash basis is rejected with `cash_basis_missing`.

## Shared-run result

Both `lx -> sy` and `sy -> lx` use one temporary shared required-data directory with the actual planner, persistence, and coverage reader. The deterministic OpenD boundary returns Put strikes `35`, `40`, and `42.5` for expirations `2026-08-21` and `2026-09-18`, spot `43.07`, and realized-volatility evidence.

- Both accounts resolve identical Put plans with window `34.456..43.07`.
- Both account orders expose the same six Put contracts.
- Exactly one fetch occurs per two-account order; the second account consumes the existing coverage.
- The request remains Put-only and requests both fixed expirations plus realized volatility.
- `run_account_outcomes(..., max_workers=2)` returns identical plan debug payloads for lx and sy.

## Validation

- `python3.12 -m pytest -q -p no:cacheprovider tests/test_candidate_filter_trace.py tests/test_sell_put_cash_total_cny.py tests/test_pipeline_fetch_read_model_boundary.py tests/test_symbol_monitoring_fetch_spec_merge.py` -> `53 passed`.
- `git diff --check` -> pass.
- `python3.12 -m compileall -q tests/test_candidate_filter_trace.py tests/test_pipeline_fetch_read_model_boundary.py` -> pass.
- Deepreview: `docs/reviews/code-review-20260723-000310.md` -> `未发现实质性问题`.

## Residual risk

- Tests prove same-process sequential reuse and concurrent plan construction. They intentionally do not claim cross-process shared-file atomicity.
- Runtime request-count/duration budget and broad regression remain S3.
- No production canary or external write was performed.

- Status: `accepted`.
