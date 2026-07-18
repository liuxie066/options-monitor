# Gateflow Aggregate Deepreview Fix — Topology and Quality

- Gate: aggregate deepreview fix
- Work unit: Combo Yield staggered/diagonal 跨期收益和资金占用归因
- Findings: ADR-01, ADR-02
- Date: 2026-07-18
- Status: fixed; pending re-review
- Artifact path: `docs/gateflow/cross-expiry-yield-capital-attribution-aggregate-fix-20260718.md`

## Fixes

- Group topology now proves Funding Put is a short Put and Participation Call is a long Call.
- Diagonal/staggered groups require Call expiration strictly after Put expiration.
- Invalid imported/legacy groups remain in canonical totals and become attribution-partial.
- Group quality now aggregates material period-total gross/net and group-efficiency availability instead of checking metadata issues alone.
- Added adversarial mislabeled-leg and incomplete-PnL quality tests.
- Regenerated dependency graph for the two new production modules.

## Validation

```text
ruff: pass
focused performance/assignment pytest: 60 passed
dependency graph: 468 production modules, 0 cycles
```

## Residual risks

| Risk | Classification |
|---|---|
| Exact intra-period transition PnL split | assigned to later evidence-capture work unit |
| Multiple Funding Put rolls | assigned to later work unit |
| Historical missing strategy provenance | data repair/later work unit; current reducer fails closed |
| Broker margin/NAV efficiency | assigned to later work unit |
