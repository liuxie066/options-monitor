# Gateflow Aggregate Deepreview Fix/Re-review — Cross-Expiry Attribution

## Gate

- Work unit: cross-expiry yield and capital attribution
- Gate: aggregate deepreview -> fix -> re-review
- Review artifact: `docs/reviews/code-review-20260718-181944.md`
- Branch: `codex/cross-expiry-attribution-post-merge-deepreview`
- Status: pass
- Artifact path: `docs/gateflow/cross-expiry-attribution-aggregate-deepreview-fix-20260718-182953.md`

## Finding Decisions and Final Status

| Finding | Decision | Fix | Re-review status |
|---|---|---|---|
| CR-01 residual tail observed quality beside unavailable PnL | accepted | Derive isolated-tail quality from gross/net period-total metric status and emit explicit metric issues | 已修复 |
| CR-02 assigned-stock metadata conflict/incomplete semantics | accepted | Compare snapshot/top-level strategy metadata, fail closed for Combo conflicts/incomplete provenance, preserve non-Combo observed-empty behavior | 已修复 |

## Regression Evidence

The new tests first failed on the merged implementation with six failures, directly reproducing both findings. After the fixes:

- focused attribution/performance: `47 passed`
- complete performance suite: `136 passed`
- ledger/Combo/assigned-stock/positions/option-positions integration: `377 passed`
- full repository after dependency graph regeneration: `2672 passed, 10 skipped`
- dependency graph: `468` production modules, `0` cycles; generator tests `2 passed`
- Ruff on performance production/tests: pass
- `python3 -m compileall -q domain src`: pass
- US/HK example config validation: pass
- US/HK example config build `--dry-run`: pass, `write_applied=false`
- `git diff --check`: pass

The first full-repository run reported only a stale generated dependency graph (`2671 passed, 10 skipped, 1 failed`). Regenerating `docs/DEPENDENCY_GRAPH.md` updated test-import counts; the subsequent complete run passed.

## Docs Decision

No public schema or user-visible attribution field was added. Existing design semantics already require unavailable residual value and metadata conflicts to fail closed. Public design docs therefore remain correct; only generated dependency evidence and Gateflow/review artifacts changed.

## Residual Risks

| Risk | Classification / owner |
|---|---|
| Exact intra-period transition PnL split | assigned to later evidence-capture work unit |
| Multiple Funding Put rolls per Participation Call | assigned to future multi-cycle attribution work unit |
| Historical missing provenance | assigned to data-repair/backfill work unit |
| Broker-margin/NAV efficiency | assigned to portfolio capital methodology work unit |

No unclassified residual risk remains. Aggregate deepreview gate passes.
