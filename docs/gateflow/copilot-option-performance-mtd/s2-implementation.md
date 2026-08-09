# Gateflow Slice 2 Implementation — PnL Decomposition and Rendering

- Work unit: `copilot-option-performance-mtd`
- Slice: `S2`
- Gate: `implementation`
- Date: 2026-07-23
- Status: complete
- Base: `c3ae873a`

## Canonical financial semantics

`build_period_performance()` now retains exact generation-time collections for:

- option allocation/missing-allocation realized facts;
- assigned-stock lifecycle facts.

The existing combined fields remain unchanged:

```text
pnl.realized_gross
pnl.realized_net
```

Additive fields now expose:

```text
pnl.option_realized_gross
pnl.option_realized_net
pnl.assigned_stock_realized_gross
pnl.assigned_stock_realized_net
```

The assigned-stock lifecycle summary consumes only its exact fact collection. It no longer
classifies facts by source event ID, so an assignment option allocation cannot leak into stock
realized PnL when both legitimately share the assignment event ID.

## User-facing report

The deterministic option-performance renderer now shows:

- MTD/YTD/natural-period label, date range, current/past status, and actual account scope;
- combined period-total PnL;
- combined, pure-option, and assigned-stock realized gross/net PnL;
- option trade cash and option fee cash;
- assignment/exercise settlement principal and fee;
- assigned-stock sale proceeds and fee;
- premium activity, option contracts, assigned shares, ending lots, sales, review and unsupported
  inventory counts;
- report-level missing evidence and warnings.

Metric status is read from the actual top-level `status` field, so `partial`, `not_observed`, and
`not_applicable` are no longer silently omitted in rendered text.

The wording explicitly states:

- period-total PnL is combined and includes unrealized change;
- combined realized contains the two displayed components;
- premium is activity;
- settlement principal and stock sale proceeds are cash flows, not PnL;
- actual option/stock fees affect the respective net component.

## Contract and documentation

- Added all component PnL and detailed cash fields to the Agent output contract.
- Updated `docs/OPTION_PERFORMANCE_DESIGN.md` with the additive identity and exact-fact ownership
  rule.
- Existing schema version and existing fields remain unchanged; the change is additive.

## Regression coverage

- Assignment month: option premium realization stays in option PnL; stock settlement fee stays
  in assigned-stock net.
- Stock-sale month: option realized is not observed; stock gross/net owns sale economics.
- Same month assignment + partial stock sale:
  - combined gross `750 = option 250 + stock 500`;
  - combined net `747 = option 250 + stock 497`;
  - assigned-stock lifecycle excludes the option `250`.
- Renderer snapshot assertions cover all-account MTD, partial evidence, component PnL, sale cash,
  sub-unit actual fees, and explicit semantics.
- Agent output contract assertions cover new PnL/cash fields.

## Validation

```text
/Volumes/Workspace/workspace/options-monitor/.venv/bin/ruff check \
  domain/domain/performance/engine.py \
  src/application/agent_tools/positions.py \
  src/application/assistant/renderer.py \
  tests/test_performance_assignment.py \
  tests/test_assistant_runtime.py \
  tests/test_option_performance_agent_tool.py

All checks passed.
```

```text
PYTHONPYCACHEPREFIX=/tmp/om-copilot-option-performance-pycache \
python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_performance_engine.py \
  tests/test_performance_assignment.py \
  tests/test_performance_valuation.py \
  tests/test_performance_service.py \
  tests/test_option_performance_agent_tool.py \
  tests/test_assistant_runtime.py \
  tests/test_agent_plugin_contract.py \
  tests/test_inbound_control.py \
  tests/test_inbound_feishu_ws.py

182 passed.
```

## Safety

- No persisted schema, ledger event, accounting rule, configuration, or runtime state changed.
- No live quote, Feishu send, production read/write, release, or deployment occurred.

## DeepReview

- Initial review: `docs/reviews/code-review-20260723-170603.md`
- Accepted fix: `docs/gateflow/copilot-option-performance-mtd/s2-review-fix.md`
- Passing re-review: `docs/reviews/code-review-20260723-170836.md`
- Gate record: `docs/gateflow/copilot-option-performance-mtd/s2-code-review.md`
