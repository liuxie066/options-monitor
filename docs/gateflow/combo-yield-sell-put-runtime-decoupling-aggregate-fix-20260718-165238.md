# Aggregate Deepreview Fix — Combo Yield / Sell Put Runtime Decoupling

- **Finding**: ADR-1
- **Decision**: accepted
- **Status**: fixed

## Fix

- Made `run_combo_yield_scan_fn`, `empty_combo_yield_summary_fn`, `materialize_empty_sell_put_artifacts_fn`, and `materialize_empty_combo_yield_artifacts_fn` required `SymbolMonitoringDependencies` fields.
- Removed optional guards from runtime orchestration.
- Updated every test composition root to wire explicit Combo and artifact dependencies.

## Validation

- affected strategy/data tests: 83 passed
- multi-tick/notification/architecture tests: 106 passed
- diff check and compileall: passed

## Residual risks

All classified: duplicate scan accepted; shared fetch failure assigned to later work unit.
