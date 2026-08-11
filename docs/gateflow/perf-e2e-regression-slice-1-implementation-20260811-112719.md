# Gateflow Implementation Artifact — perf-e2e-regression slice-1

- Gate: `implementation`
- Work unit: `perf-e2e-regression`
- Slice: `slice-1`
- Artifact path: `docs/gateflow/perf-e2e-regression-slice-1-implementation-20260811-112719.md`
- Branch: `perf-e2e-regression`
- Baseline: `db2c361e`

## Objective

Add a real end-to-end regression test proving that a Combo Yield pair opened through the trade resolver is
persisted to SQLite and then correctly attributed by the period performance service. This closes the gap where
attribution logic was only exercised against hand-built in-memory `TradeEvent` objects.

## Scope

Allowed changes: `tests/test_performance_resolver_projection_e2e.py` only. No production code changes.

## Changed files

- `tests/test_performance_resolver_projection_e2e.py` (new)

## Implementation decisions

1. Open path uses the real production chain: `resolve_trade_deal` -> `record_normalized_trade_event` ->
   `persist_trade_event` -> `persist_trade_event_object` -> SQLite persistence -> lot projection. No mocks in the
   ledger write path.
2. Close/expire path uses `persist_trade_event_object` directly with explicit `target_lot_id` and a
   `strategy_snapshot` in `raw_payload`. Rationale: a zero-price expiry through the resolver enters the
   lifecycle-pending flow instead of producing a plain realized close, which is out of scope for this regression.
3. The resolver's `_enrich_combo_yield_open` returns `combination_relation_pending` diagnostics for both legs and
   does not block the open; the test therefore asserts `status == "applied"` and does not assert pair-relation state.
4. Attribution assertions match the existing hand-built attribution baseline (put 500 / call 400 / funded 400 /
   surplus 100 / participation 300 / group 800 / total 800 / residual 0.0), so the two test layers stay consistent.
5. Timezone is fixed to Asia/Shanghai and `NOW_MS` is pinned so period selection and time bucketing are deterministic.

## Validation

Focused run:

```bash
PYTHONPYCACHEPREFIX=<tmp-cache> <repo-root>/.venv/bin/python -m pytest tests/test_performance_resolver_projection_e2e.py -q -p no:cacheprovider
# 1 passed
```

Regression run (attribution, period service, resolver close, assigned-stock intake):

```bash
PYTHONPYCACHEPREFIX=<tmp-cache> <repo-root>/.venv/bin/python -m pytest \
  tests/test_performance_resolver_projection_e2e.py \
  tests/test_performance_strategy_attribution.py \
  tests/test_performance_service.py \
  tests/test_trades_resolver_close.py \
  tests/test_assigned_stock_sale_intake.py \
  -q -p no:cacheprovider
# 85 passed
```

## Docs decision

No user-facing docs change: this is a test-only addition with no public contract, output path, or CLI change.

## Residual risks

- Happy-path only: no rejected/validation-failed opens, lifecycle-pending expiries, or `partial_data` attribution
  paths. Classified: covered by later work unit if such coverage is ever required; not part of this work unit's
  success signal.
- `_lot_record_id` locates lots by exact float strike comparison; safe for the exact values used here, brittle for
  computed strikes. Classified: fixed in current slice is not needed; noted for future test authors.
- No production code was touched, so this slice introduces no production behavior risk.

## Completion status

`implementation complete` for slice-1. Validation green (1 passed focused, 85 passed regression). Next gate:
`code review`.
