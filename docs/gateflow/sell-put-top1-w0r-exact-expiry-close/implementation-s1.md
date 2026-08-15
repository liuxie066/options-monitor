# Gateflow Implementation — Sell Put Top1 W0R Exact-Expiration Close Boundary

- Gate: `implementation`
- Work unit: `sell-put-top1-w0r-exact-expiry-close`
- Slice: `S1 strict exact-expiration close source`
- Branch/base: `feat/sell-put-top1-w0r-exact-expiry-close` / `origin/main@813ec6f8021148ff6d152ff4ee4f5c39e36897fc`
- Accepted plan commit: `8d352222`
- Artifact path: `docs/gateflow/sell-put-top1-w0r-exact-expiry-close/implementation-s1.md`
- Status: implementation complete; Kimi code review passed with no findings

## Changed behavior

- Added `FutuGateway.get_exact_expiration_close()` as a dedicated read-only SDK boundary; the permissive generic history method remains unchanged.
- Forced one exact date, `K_DAY`, `NONE`, `time_key/close`, `max_count=2`, and an initial null continuation key.
- Required the documented SDK DataFrame shape and exact `code/time_key/close` columns before treating zero rows as successful absence.
- Rejected malformed return values, continuation, multiple rows, code/date conflicts, non-canonical time, and non-positive/non-finite close through the existing gateway error mapping.
- Returned only normalized `code`, requested `expiration`, and float `close`; no provider display data or receipt semantics leak into the fact.
- Updated the W0R preflight to `SDK/project source green; live unknown`, preserving overall `runtime_no_go`.

## Changed files

- `src/infrastructure/futu_gateway.py`
- `tests/test_futu_gateway_minimal.py`
- `docs/performance/sell-put-top1-capability-preflight-20260814.md`
- `docs/DEPENDENCY_GRAPH.md`
- this implementation artifact

The dependency graph changed only because the new tests import three additional infrastructure symbols; production edges and cycle count are unchanged.

## Validation

- `pytest ... tests/test_futu_gateway_minimal.py tests/test_short_vol_metrics.py tests/test_strategy_lab_top1_research.py` — `51 passed in 2.59s`.
- Ruff on changed Python files — passed.
- `scripts/generate_dependency_graph.py --check` — current, `production_modules=590 cycles=0`.
- `git diff --check` — passed.
- Full repository suite — `4900 passed, 10 skipped`; its only sandbox failure was the existing HTTP test's denied `127.0.0.1` bind. Re-running that exact test outside the network sandbox passed (`1 passed`). A temporary worktree `.venv` symlink was used for subprocess entrypoint tests and removed after validation.

No validation imported/started OpenD or queried provider/account state.

## Docs decision

The capability preflight is updated because its current source-status claim changed. No product/operator documentation changed because there is no public command or runtime consumer.

## Residual risks and owners

- Live provider shape, exact-expiration availability, quota headroom, and latency — separately authorized W0R live probe.
- Domain symbol conversion, timing/deadline, absence reason, rate-limit use, retry/dedupe, receipt sealing/storage, and publication — remaining W5 runner.
- Account fee-plan, calendar, terms, observation, and overall readiness — remaining W0R/W7 work.

No residual risk is unclassified.

## Next gate

Accepted slice commit, then aggregate Kimi DeepReview.
