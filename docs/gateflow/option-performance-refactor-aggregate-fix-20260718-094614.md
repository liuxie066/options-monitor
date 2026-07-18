# Gateflow Fix Artifact — Option Performance Refactor Aggregate Deepreview

- **Gate**: aggregate deepreview fix
- **Work unit**: `option-performance-refactor`
- **Source review**: `docs/reviews/code-review-20260718-094014.md`
- **Created at**: 2026-07-18 09:46:14 CST
- **Status**: fix-complete; awaiting aggregate re-review
- **Artifact path**: `docs/gateflow/option-performance-refactor-aggregate-fix-20260718-094614.md`

## Accepted Findings and Fixes

### ADR-01 — exact account ownership at both bridge inputs

- PnL and cash fact validators now receive the requested account and require the PM payload's `account` to match exactly.
- Option evidence now requires `scope.account` to equal the requested account. Empty/aggregate scope fails with `option_performance_account_mismatch`.
- Added PnL/cash regressions for foreign PM facts and aggregate option reports.

### ADR-02 — exact Asia/Shanghai period boundary

- Both PM fact contracts now require `period.timezone == "Asia/Shanghai"`.
- Both option report contracts require `period.reporting_timezone == "Asia/Shanghai"`.
- Added PnL/cash UTC mismatch regressions and updated real-contract fixtures to include the reporting timezone.

### ADR-03 — assigned-stock lifecycle review debt degrades report quality

- `context="assigned_stock"` diagnostics from the opening/ending lifecycle projections now follow the same account/broker-wide relevance policy as valuation diagnostics.
- This intentionally surfaces unresolved historical inventory facts and current invalid sales because either can affect opening inventory, ending inventory, cash, or realized PnL.
- Added service-level regressions proving:
  - a historical assignment without stock settlement blocks proven-zero promotion;
  - an invalid current-period sale with complete marks and FX leaves the known subtotal visible but makes top-level report quality partial with the exact lifecycle warning.

### ADR-04 — partial report quality remains binding in bridges

- PnL/cash option evidence is usable only when both the nested metric and `report.quality.status` are `observed`, and the CNY amount exists.
- Partial reports retain their known metric amount/status as audit evidence but produce null bridge option and residual decomposition steps.
- Added PnL/cash regressions for observed nested metrics inside partial reports.

## Changed Files

Production:

- `domain/domain/performance/engine.py`
- `src/application/portfolio_pnl_bridge.py`
- `src/application/portfolio_cash_bridge.py`

Tests:

- `tests/test_performance_assignment.py`
- `tests/test_portfolio_pnl_bridge.py`
- `tests/test_portfolio_cash_bridge.py`
- `tests/test_portfolio_agent_tool.py`

Docs:

- `docs/OPTION_PERFORMANCE_DESIGN.md`
- `docs/reviews/code-review-20260718-094014.md` (ADR-04 added when the downstream consequence was proven during the fix loop)
- this fix artifact

No release/version file, production config, notification path, Feishu state, broker state, or option-position state was modified by this fix.

## Validation

Focused fix suite:

```text
python3 -m pytest \
  tests/test_performance_assignment.py \
  tests/test_portfolio_pnl_bridge.py \
  tests/test_portfolio_cash_bridge.py -q

25 passed
```

Expanded aggregate-risk suite:

```text
python3 -m pytest \
  tests/test_performance_engine.py \
  tests/test_performance_service.py \
  tests/test_performance_assignment.py \
  tests/test_performance_capital.py \
  tests/test_performance_reconciliation.py \
  tests/test_portfolio_pnl_bridge.py \
  tests/test_portfolio_cash_bridge.py \
  tests/test_portfolio_agent_tool.py \
  tests/test_option_performance_agent_tool.py \
  tests/test_option_performance_cli.py -q

111 passed
```

`git diff --check` passed for the modified bridge paths and review/design artifacts at fix time.

## Docs Decision

The Option Performance v1 design now states the exact portfolio bridge boundary: per-account ownership, shared `Asia/Shanghai` cutoff, and report-level quality gating. `docs/AGENT_INTEGRATION.md` was intentionally not edited because it contains unrelated dirty Feishu changes and is excluded from this aggregate gate's staging scope.

## Residual Risk

- **fixed in current aggregate loop**: ADR-01 through ADR-04.
- **assigned to operational follow-up**: existing assigned-stock lifecycle review rows may newly make reports partial; operators must repair the source facts rather than suppress warnings.
- **assigned to external owner**: PM endpoint runtime conformance; OM now rejects missing/wrong account and timezone contracts.
- **assigned to later work unit**: legacy adapter removal after the migration window.
- **accepted v1 design limitation**: separate FX-attribution decomposition remains out of scope.

No unclassified residual risk remains.

## Completion Status

- **Fix result**: pass
- **Finding status before re-review**: ADR-01 through ADR-04 implemented
- **Current gate / next entry point**: aggregate deepreview re-review
