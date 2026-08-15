# Gateflow Implementation — Sell Put Top1 W0R History K-Line Quota Boundary

- Gate: `implementation`
- Work unit: `sell-put-top1-w0r-history-quota`
- Slice: `S1 strict quota and limiter boundary`
- Branch/base: `feat/sell-put-top1-w0r-history-quota` / `origin/main@0da901b30cd26242636b9ec967b8aa281f61937c`
- Accepted plan commit: `0fc8bd6d`
- Artifact path: `docs/gateflow/sell-put-top1-w0r-history-quota/implementation-s1.md`
- Status: implementation complete; Kimi code review passed with no findings

## Changed behavior

- Added `FutuGateway.get_history_kl_quota()` as a zero-argument, read-only SDK boundary that always requests detailed facts.
- Added strict normalization for provider return shape, ret type/value, non-negative integral counts, exact detail count, unique canonical codes, and exact `%Y-%m-%d %H:%M:%S` timestamps.
- Returned only deterministic `used_quota`, `remain_quota`, and sorted `code/request_time` facts; provider display names are dropped.
- Added canonical `runtime.opend_rate_limits.history_kline` resolution with provider-documented `60 calls / 30s` defaults and `30s` max wait.
- Kept existing candidate fetch/discovery kwargs unchanged; no caller or runtime side effect was added.
- Updated the W0R preflight to `SDK/project source green; live unknown`, preserving overall `runtime_no_go`.

## Changed files

- `src/infrastructure/futu_gateway.py`
- `src/application/opend_fetch_config.py`
- `tests/test_futu_gateway_minimal.py`
- `tests/test_opend_batch_config.py`
- `tests/test_global_liquidity_filters.py`
- `docs/performance/sell-put-top1-capability-preflight-20260814.md`
- `docs/DEPENDENCY_GRAPH.md`
- `docs/dependency_graph.mmd`
- this implementation artifact

The dependency graph was already stale against current `origin/main` after intervening merged source/test changes; running the required generator produced only mechanical graph facts and now reports 590 production modules with zero cycles.

## Validation

- `pytest ... tests/test_futu_gateway_minimal.py tests/test_opend_batch_config.py tests/test_global_liquidity_filters.py` — `58 passed in 0.72s`.
- `pytest ... tests/test_short_vol_metrics.py tests/test_fetch_market_data_opend_explicit_expirations.py tests/test_required_data_prefetch_inprocess.py tests/test_symbol_monitoring_fetch_spec_merge.py tests/test_research.py` — `114 passed in 3.04s`.
- Ruff on all changed Python files — passed.
- `scripts/generate_dependency_graph.py --check` — current, `production_modules=590 cycles=0`.
- `git diff --check` — passed.
- Full repository suite — `4889 passed, 10 skipped`; the initial isolated-worktree run had nine environment-only failures: eight entrypoint tests could not find a worktree-local `.venv`, and one HTTP test could not bind loopback inside the sandbox. Reusing the project venv through a temporary worktree symlink made the complete entrypoint file pass (`10 passed`), and rerunning the HTTP test outside the network sandbox passed (`1 passed`). The temporary symlink was removed.

No test or validation imported/started OpenD or queried provider/account state.

## Docs decision

The capability preflight is updated because its current source-status claim changed. No product/operator documentation changed because there is no public command or runtime consumer.

## Residual risks and owners

- Live quota/OpenD evidence — assigned to a separately authorized W0R live probe.
- Quota sufficiency, unique-owner demand, persistence, retry/dedupe, and the W5 provider runner — assigned to the remaining W5 work.
- Production coexistence and bounded scheduling — assigned to remaining W5/W7 readiness.
- Account fee-plan, calendar, exact-expiration close/terms, observation, and capacity — assigned to later W0R work units.

No residual risk is unclassified.

## Next gate

Accepted slice commit, then aggregate Kimi DeepReview.
