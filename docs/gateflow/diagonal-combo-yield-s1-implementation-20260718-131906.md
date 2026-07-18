# Gateflow Implementation Artifact — Diagonal Combo Yield S1

## Gate State

- Work unit: true diagonal Combo Yield lifecycle
- Slice: S1 — diagonal opening contract and shadow candidate output
- Previous gate: accepted plan commit `cce490ff`
- Current decision: implementation complete; enter mandatory S1 code review
- Next entry point: `code review (S1)`

## Implemented Scope

1. Added an additive expiry-structure contract:
   - `same_expiry` remains the default.
   - `diagonal` requires explicit `call.min_dte` and `call.max_dte`.
   - `min_expiry_gap_days` enforces a positive calendar gap.
2. Preserved independent Put and Call fetch/filter windows for diagonal mode while retaining the existing same-expiry derivation behavior.
3. Extended pair validation and candidate generation so diagonal pairs require `call_expiration > put_expiration` and satisfy the configured minimum gap.
4. Added dual-expiry output fields, a canonical pair fingerprint, and an account-scoped group ID when account evidence exists.
5. Kept known opening economics populated, but made same-horizon terminal/scenario fields null for diagonal rows rather than treating unknown Call residual value as zero.
6. Added expiry-gap ranking tie-breaking without making unsupported terminal fields attractive.
7. Updated alert rendering and linked-call formatting to show the actual Call expiry and explicitly state that Put-expiry Call residual value is not predicted.
8. Preserved same-expiry aliases (`expiration`, `dte`) and default behavior for compatibility.

## Files Changed

- `configs/system.json`
- `domain/domain/engine/__init__.py`
- `domain/domain/engine/yield_enhancement.py`
- `src/application/config_defaults.py`
- `src/application/config_validator.py`
- `src/application/render_yield_enhancement_alerts.py`
- `src/application/required_data_planning.py`
- `src/application/required_data_prefetch_planning.py`
- `src/application/sell_put_call_helper.py`
- `src/application/yield_enhancement_config.py`
- `tests/test_render_yield_enhancement_alerts.py`
- `tests/test_required_data_prefetch_inprocess.py`
- `tests/test_sell_put_linked_call_helper.py`
- `tests/test_sell_put_yield_enhancement_required_data_planning.py`
- `tests/test_sell_put_yield_enhancement_validate_config.py`

## Validation

```text
python3 -m pytest \
  tests/test_sell_put_linked_call_helper.py \
  tests/test_sell_put_yield_enhancement_required_data_planning.py \
  tests/test_sell_put_yield_enhancement_validate_config.py \
  tests/test_render_yield_enhancement_alerts.py
```

Re-review validation expanded the command with `tests/test_required_data_prefetch_inprocess.py`.

Result: `69 passed`.

Additional checks:

- `python3 -m py_compile ...` passed for changed Python modules/tests.
- `git diff --check` passed.
- `configs/system.json` diff contains only the additive expiry-structure and Call DTE default fields for US/HK templates.

## Covered Scenarios

- later Call expiry accepted;
- same/earlier Call expiry rejected;
- configured minimum expiry gap enforced;
- independent Put/Call DTE windows;
- in-process prefetch preserves the later diagonal Call horizon;
- diagonal terminal metrics remain null;
- canonical pair fingerprint and account-scoped group ID;
- actual Call expiry propagated to linked-call output;
- renderer shows both horizons and does not display terminal scenario predictions;
- invalid diagonal config fails closed;
- zero diagonal Call DTE bounds fail at config validation;
- existing same-expiry tests remain green.

## Non-Goals Preserved

- No trade intake/projection persistence changes (S2).
- No assignment lineage or assigned-stock reporting changes (S3).
- No Close Advice action synthesis changes (S4).
- No production config/runtime state mutation.
- No notification delivery or broker-facing writes.

## Residual Risk Entering Review

- S1 tests exercise candidate/config/render surfaces but not yet the later structured/manual intake handoff; that is owned by S2.
- Full repository regression remains for later slice/aggregate gates.
