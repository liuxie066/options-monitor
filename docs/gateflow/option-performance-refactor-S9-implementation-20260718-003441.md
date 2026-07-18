# Gateflow Implementation Artifact — Option Performance Refactor S9

- **Gate**: implementation
- **Work unit**: `option-performance-refactor`
- **Slice**: S9 — Portfolio PnL and Cash Bridges
- **Created at**: 2026-07-18 00:34:41 UTC
- **Status**: implementation-complete; awaiting code review
- **Artifact path**: `docs/gateflow/option-performance-refactor-S9-implementation-20260718-003441.md`

## Scope and Decisions

Implemented the approved replacement of the mixed legacy capital bridge with two independent accounting equations.

### PnL bridge

- Added primary `portfolio_pnl_bridge` backed by PM `/analysis/capital-facts` and `option_performance_report.pnl.period_total_net`.
- Requires CNY capital facts, actual PM end date, aligned option report period/account, an observed CNY metric, and complete net-fee/FX evidence.
- Uses option period-total net PnL, not option cash. Assignment/stock-settlement principal cannot enter the PnL equation.
- Separates `portfolio_and_other_pnl` from an explicit `reconciliation_residual` rather than hiding PM basis mismatch.

### Cash bridge

- Added primary `portfolio_cash_bridge` backed only by PM `/analysis/cash-facts` and `option_performance_report.cash.total_cash_change_net`.
- Requires opening cash, external cash flow, ending cash, CNY, actual PM end date, and aligned observed option cash evidence.
- Does not assume an undocumented `period_cash_change` is mandatory: when PM supplies it the bridge validates it; otherwise the bridge derives the period cash change from opening/external/ending cash.
- Preserves all six option/assignment cash components, including stock settlement and stock sale fee cash, as evidence.
- Never substitutes opening/ending assets for cash balances.

### Public tool boundary and compatibility

- Registered `portfolio_pnl_bridge` and `portfolio_cash_bridge` in the canonical portfolio pure-read toolset.
- PM transport/HTTP/404 failures become per-account structured unavailable facts; missing facts and incomplete option evidence remain null, never zero.
- Retained `portfolio_capital_bridge` as an explicitly deprecated compatibility tool and left its legacy implementation unchanged.
- Updated capability and integration docs to make the two new bridges primary and document the old bridge as rollback-only.
- Preserved the unrelated existing Feishu ACK paragraph edit in `docs/AGENT_INTEGRATION.md`.

## Changed Files

Production:

- `src/application/portfolio_pnl_bridge.py`
- `src/application/portfolio_cash_bridge.py`
- `src/application/agent_tools/portfolio.py`

Tests:

- `tests/test_portfolio_pnl_bridge.py`
- `tests/test_portfolio_cash_bridge.py`
- `tests/test_portfolio_agent_tool.py`

Docs:

- `docs/OM_AGENT_CAPABILITY_MAP.md`
- `docs/AGENT_INTEGRATION.md` (S9 bridge sections only; unrelated Feishu change excluded from S9 ownership)

`src/application/portfolio_capital_bridge.py` and `tests/test_portfolio_capital_bridge.py` did not require code changes; compatibility behavior remains covered by the focused suite.

## Validation

Passed approved focused suite:

```text
python3 -m pytest \
  tests/test_portfolio_pnl_bridge.py \
  tests/test_portfolio_cash_bridge.py \
  tests/test_portfolio_agent_tool.py \
  tests/test_portfolio_capital_bridge.py -q

46 passed
```

Passed focused Ruff and syntax checks:

```text
python3 -m py_compile \
  src/application/portfolio_pnl_bridge.py \
  src/application/portfolio_cash_bridge.py \
  src/application/agent_tools/portfolio.py

python3 -m ruff check \
  src/application/portfolio_pnl_bridge.py \
  src/application/portfolio_cash_bridge.py \
  src/application/agent_tools/portfolio.py \
  tests/test_portfolio_pnl_bridge.py \
  tests/test_portfolio_cash_bridge.py \
  tests/test_portfolio_agent_tool.py \
  tests/test_portfolio_capital_bridge.py
```

`git diff --check` passed for all S9-owned paths.

Additional registry/Copilot integration validation produced `152 passed, 1 failed`. The single failure is a stale S8-era smoke-test monkeypatch that still expects `src.application.agent_tools.analysis.get_exchange_rates`; neither that source nor that test is modified by S9. It is classified below for the approved S10 whole-suite cutover gate rather than expanding S9.

## Docs Decision

Updated both approved public capability documents. No release version or changelog change belongs to S9.

## Residual Risks and Uncovered Areas

| Risk / area | Classification |
|---|---|
| PM `/analysis/cash-facts` is an external contract not implemented in this repository | covered by current S9 structured unavailable behavior; external PM implementation/integration is a separate owner |
| One option performance report is loaded per available PM account | accepted correctness-first bounded behavior in current slice; benchmark/cache remains a later work unit only if measured |
| Current-period PnL can remain partial when persisted valuation/FX evidence is absent because bridge refresh is intentionally disabled | intended safety behavior in current slice; evidence capture is the explicit operator workflow |
| Stale `test_agent_plugin_smoke` dependency patch target after S8 migration | covered by later approved S10 whole-suite validation/cutover cleanup |
| Repository-wide legacy consumer zero-check and rollback/removal entry point | covered by later approved S10 |

No unclassified residual risk remains.

## Completion Status

- **Implementation**: pass
- **Blocking open questions**: none
- **Current gate / next entry point**: S9 code review using `deepreview`
