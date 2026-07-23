# Gateflow Slice 2 Review Fix

- Work unit: `copilot-option-performance-mtd`
- Slice: `S2`
- Gate: `fix`
- Review artifact: `docs/reviews/code-review-20260723-170603.md`
- Status: fix complete; pending re-review

## Finding decision

### S2-DR-01 — accepted — fixed

The option-performance renderer reused an integer-only native-currency formatter. Actual fees
below one currency unit could therefore appear as zero even though the canonical metric retained
the correct value.

The fix is deliberately local to this report:

- `_performance_metric_text()` now uses a performance-specific native-currency formatter with
  two decimal places;
- other report renderers keep their existing formatting;
- the renderer regression uses `USD -0.35` and `USD -2.15` fees and asserts that both exact
  values remain visible.

## Validation

```text
ruff: All checks passed.
pytest: 182 passed.
```

## Safety

- No accounting value, event, persisted data, configuration, or production state changed.
- This is a deterministic presentation correction only.
