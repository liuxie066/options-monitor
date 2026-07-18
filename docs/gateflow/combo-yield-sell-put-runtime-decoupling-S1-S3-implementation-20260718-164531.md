# Gateflow Implementation Artifact — Combo Yield / Sell Put Runtime Decoupling

- **Gate**: implementation
- **Work unit**: Combo Yield 与 Sell Put 的运行耦合审计与解耦
- **Slices**: S1 independent orchestration; S2 caller/prefetch gating; S3 compatibility/docs
- **Status**: implementation complete, awaiting code review

## Changed files

- `src/application/symbol_monitoring.py`
- `src/application/pipeline_symbol.py`
- `src/application/sell_put_steps.py`
- `src/application/combo_yield_steps.py`
- `src/application/required_data_prefetch_planning.py`
- `src/application/multi_tick/required_data_prefetch.py`
- `tests/test_symbol_monitoring_fetch_spec_merge.py`
- `tests/test_required_data_prefetch_inprocess.py`
- `docs/STRATEGY_ARCHITECTURE.md`

## Decisions implemented

1. `run_symbol_monitoring()` computes Combo Yield enablement independently from Sell Put enablement.
2. Sell Put and Combo Yield are invoked as separate injected application dependencies in stable summary order.
3. A symbol-level Combo Yield facade owns policy/window/liquidity/event-risk resolution and low-level Combo runner invocation.
4. Sell Put runner no longer calls Combo Yield.
5. Strategy exceptions are isolated at symbol orchestration; failing/disabled strategies materialize empty current artifacts to prevent stale recommendations.
6. Combo Yield separate execution uses its own funding-put scan; Sell Put candidates are used only for legacy inline attachment and only when Sell Put is enabled.
7. Strategy prefetch includes Combo Yield put and call requirements even when Sell Put recommendation is disabled.
8. Canonical `required_data_planning.py` remains unchanged.

## Validation

```text
python3 -m pytest tests/test_symbol_monitoring_fetch_spec_merge.py tests/test_combo_yield_steps.py tests/test_required_data_prefetch_inprocess.py tests/test_sell_put_yield_enhancement_required_data_planning.py -q
47 passed in 0.62s
```

## Docs decision

Updated `docs/STRATEGY_ARCHITECTURE.md` to distinguish shared funding-put capabilities/config from independent runtime step ownership.

## Residual risks

- Shared required-data acquisition failure remains symbol-level: assigned to later work unit if independent fetch failure domains are required.
- Duplicate funding-put scans remain accepted current cost.
- Legacy inline mode cannot attach Combo output when Sell Put is disabled/failed; separate output remains canonical and independent.
- Existing dirty changes overlap some touched files; accepted-slice commit requires precise ownership handling before commit.
