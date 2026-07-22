# Gateflow Implementation Artifact — S1 Fee Truth and Fail-Closed Safety

- **Gate**: implementation
- **Work unit**: `close-advice-bug-boundaries`
- **Slice**: S1 — shared fee truth and Close Advice fee fail-closed
- **Baseline**: accepted plan commit `d62c2289`
- **Artifact path**: `docs/gateflow/close-advice-bug-boundaries/s1-implementation.md`
- **Status**: implementation and code-review loop complete; ready for accepted-slice commit

## Scope and changed files

- `domain/domain/fee_calc.py`
  - updated the dated Futu US option fee components;
  - added OCC cap, settlement, CAT, current ORF/SEC/TAF values;
  - added the HK exact-HKD-0.01 tariff waiver while keeping Tier 1 as a conservative upper bound;
  - made currency dispatch reject missing/unsupported currencies instead of defaulting to USD;
  - added dated stable basis tokens.
- `domain/domain/close_advice.py`
  - added the pure `apply_fee_economic_safety()` postcondition gate;
  - preserved existing thresholds/actions while suppressing actions with unusable or non-positive applicable economics.
- `src/application/close_advice_runner.py`
  - validates Futu broker/currency/contracts/multiplier before fee calculation;
  - projects fee status/basis, lifetime net P&L, and long net-close proceeds;
  - invokes the domain safety gate before existing action mapping and combo aggregation.
- `src/application/agent_tools/close_advice_read_impl.py`
  - exposes and numerically normalizes the new public fee diagnostics.
- `src/application/agent_tools/analysis.py`
  - carries the same fields into Close Advice analysis snapshots.
- `docs/CLOSE_ADVICE_CONTRACT.md`
  - documents schedule estimates, HK upper-bound semantics, strict evidence, and the action-safety contract.
- `tests/test_fee_calc.py`, `tests/test_close_advice_domain.py`, `tests/test_close_advice_runner.py`
  - cover formulas, all failure states, short/take-profit/salvage economics, public fields, and production-shaped broker evidence.

No scanner threshold, ranking, notification, config, state-write, or broker-facing module changed.

## Decisions and invariants

- USD uses `schedule_estimate` rather than claiming exactness because account fixed/tiered package selection is absent.
- HKD uses `conservative_estimate` with the Tier-1 upper bound; price HKD 0.01 gets the documented tariff waiver.
- `schedule_estimate` and `conservative_estimate` are usable fee evidence; all other states fail closed only for otherwise-actionable rows.
- Short profit capture and long take-profit require positive net lifetime P&L.
- Long salvage requires positive net liquidation proceeds and may retain negative lifetime P&L.
- Existing holds remain holds even when fee evidence is unavailable.
- Missing broker is not guessed; production `open_positions_min` already carries the broker, and test fixtures were made production-shaped.

## Validation

Baseline before implementation:

```text
156 passed, 1 warning
```

Post-implementation focused command:

```bash
PYTHONPYCACHEPREFIX=/tmp/close_advice_s1 python3.12 -m pytest \
  tests/test_fee_calc.py \
  tests/test_close_advice_domain.py \
  tests/test_close_advice_runner.py \
  tests/test_sell_put_linked_call_helper.py \
  tests/test_sell_call_strategy_unification.py \
  tests/test_candidate_filter_trace.py \
  tests/test_assigned_stock_quotes.py \
  tests/test_positions_reporting.py \
  -q -p no:cacheprovider
```

Final focused result:

```text
167 passed, 1 warning
```

The warning is the existing legacy Tick renderer deprecation warning. `git diff --check` also passed.

## Docs decision

Updated `docs/CLOSE_ADVICE_CONTRACT.md` because fee status/basis and net-close proceeds are public diagnostic contracts. No PRD, config, VERSION, CHANGELOG, or notification documentation changed.

## Residual risks

- **Assigned to later work unit**: actual USD account platform-package selection is unavailable; current output is explicitly a fixed-package schedule estimate.
- **Assigned to later work unit**: HK instrument tariff class is unavailable; current output is explicitly a Tier-1 upper bound.
- **Covered by S2**: lifecycle rows still use the pre-existing quote/date path until the next approved slice.
- **Assigned to later strategy work**: midpoint execution/slippage risk is unchanged.

No residual risk is unclassified.
