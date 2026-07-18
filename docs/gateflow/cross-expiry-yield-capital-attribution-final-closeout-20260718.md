# Gateflow Final Closeout — Cross-Expiry Yield and Capital Attribution

## Gate

- Work unit: cross-expiry yield and capital utilization attribution for diagonal/staggered Combo Yield
- Current gate: final closeout
- Completion status: final closeout pass
- Artifact path: `docs/gateflow/cross-expiry-yield-capital-attribution-final-closeout-20260718.md`
- Branch: `codex/cross-expiry-yield-capital-attribution-s2-capture`
- Stacked base: `codex/diagonal-combo-yield-lifecycle`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/78`

## What Changed

1. Added the additive `option_strategy_attribution.v1` report payload without changing canonical top-level cash, PnL, capital or portfolio bridge totals.
2. Added stable strategy-group and lifecycle identities for:
   - Funding Put cycle: `funding_cycle:<put-lot-id>`
   - Participation Call lifecycle: `participation:<call-lot-id>`
   - Assigned stock lifecycle: `assigned_stock:<stock-lot-id>`
   - Residual Call tail: `residual_tail:<group-id>:<put-close-event-id>`
3. Kept Call premium debit as the Participation Call lifecycle cost basis. Put credit funding is reported separately as a lifetime opening funding snapshot and is not deducted again from a Funding Put or Call PnL cycle.
4. Added period-scope PnL attribution for Funding Put, Participation Call, assigned stock and total strategy group, with native-currency conservation for realized, opening/ending unrealized and period total gross/net.
5. Added risk-capital-days attribution:
   - Short Put: strike × multiplier × remaining contracts
   - Long Call: remaining opening premium debit
   - Assigned stock: remaining stock cost basis
   - Average incremental capital: capital-days / effective period days
   - Annualized efficiency: same-scope period total net PnL / capital-days × 365
6. Added residual-tail fail-closed behavior: a report crossing the Put close boundary does not assign the entire Call PnL to the tail without an exact transition mark.
7. Added topology and provenance validation so malformed or conflicting Combo Yield groups fail closed for attribution while canonical totals remain available.
8. Preserved observed empty attribution for ordinary non-Combo reports and preserved attribution summaries when row serialization is disabled.

## Verification

- PR review focused validation: `41 passed`
  - `tests/test_performance_attribution.py`
  - `tests/test_performance_strategy_attribution.py`
  - `tests/test_performance_capital.py`
  - `tests/test_performance_engine.py`
- Aggregate performance/public/bridge validation: `197 passed`
- Aggregate ledger/Combo/position validation: `261 passed`
- Ruff: pass
- Dependency graph: `468` production modules, `0` cycles
- `git diff --check`: pass
- Draft PR state after final PR-review push:
  - Draft: yes
  - Mergeable: `MERGEABLE`
  - Head commit: `1438737b1962cf55abc08f52e26a1e1a8d43bc66`
  - GitHub checks: no checks reported

## Documentation Updates

- Updated `docs/OPTION_PERFORMANCE_DESIGN.md` with attribution semantics and public payload behavior.
- Updated `docs/STRATEGY_ARCHITECTURE.md` with lifecycle ownership and cross-expiry boundaries.
- Updated `docs/DEPENDENCY_GRAPH.md` and `docs/dependency_graph.mmd` for the new performance attribution module.
- Added complete Gateflow goal, plan, implementation, fix, review, PR-review and closeout artifacts.

## Finding Status

- Plan review: all accepted findings fixed and re-reviewed.
- S1 code review: accepted lifecycle-allocation identity finding fixed and re-reviewed.
- S2 code review: accepted non-Combo coverage, stale-group, conservation and assigned-stock identity findings fixed and re-reviewed.
- S3 code review: pass, no accepted findings.
- Aggregate deepreview: accepted topology validation and group-quality findings fixed and re-reviewed.
- PR review: pass, no material findings.
- Open blocking findings: none.

## Remaining Risks and Owners

1. Exact Call PnL split for report windows crossing Put close requires a transition mark.
   - Classification: assigned to later work unit.
   - Owner/destination: Option Performance evidence-capture work unit.
   - Current control: `transition_mark_required` fail-closed result.
2. Multiple consecutive Funding Put cycles funding one Participation Call are not supported.
   - Classification: assigned to later work unit.
   - Owner/destination: future multi-cycle Combo Yield attribution work unit.
   - Current control: topology requires exactly one Funding Put and one Participation Call.
3. Historical events without strategy provenance are not attributed automatically.
   - Classification: assigned to later work unit.
   - Owner/destination: data repair/backfill work unit.
   - Current control: provenance absence or conflict fails closed.
4. Broker-margin and NAV-based return are not represented by the risk-capital-days metric.
   - Classification: assigned to later work unit.
   - Owner/destination: portfolio capital methodology work unit.
5. The Draft PR is stacked on `codex/diagonal-combo-yield-lifecycle`.
   - Classification: explicit human-controlled integration dependency.
   - Owner/destination: repository owner during merge/retarget.
   - Current control: PR body states the stacked base explicitly.
6. GitHub Actions reported no checks for the head branch.
   - Classification: known CI coverage gap, not a code finding.
   - Owner/destination: repository CI configuration/maintainer.
   - Current control: local focused and aggregate validations passed.

## Issue Link Status

- This work unit was initiated directly by the user and is not linked to a GitHub issue.
- No closing keyword or issue closeout comment is applicable.

## Next Entry Point

- Gateflow work unit is complete at `final closeout pass`.
- User reviews Draft PR #78.
- Before merge, either land the stacked base branch/PR first or deliberately retarget PR #78 after the base work is available on the intended integration branch.
- Marking ready, requesting reviewers, approving, merging or external commenting remains a separate user-authorized action.
