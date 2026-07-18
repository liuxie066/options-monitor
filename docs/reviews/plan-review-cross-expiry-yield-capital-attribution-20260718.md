# Plan Review — 跨期收益和资金占用归因

- Gate: plan review
- Work unit: Combo Yield staggered/diagonal 跨期收益与资金占用归因
- Reviewed artifact: `docs/gateflow/cross-expiry-yield-capital-attribution-plan-20260718.md`
- Date: 2026-07-18
- Verdict: pass after reviewed fixes
- Artifact path: `docs/reviews/plan-review-cross-expiry-yield-capital-attribution-20260718.md`

## Review lenses applied

- Architecture boundary: canonical ledger allocation remains PnL owner; performance owns read-model attribution; application does not rematch lots.
- Execution/lifecycle: reports can begin after both legs opened, cross the Put transition, include partial closes, assignment, or residual Call only.
- Compatibility: additive payload must not change existing totals, bridge inputs or current core schema behavior.
- Testing/observability: malformed topology and unavailable transition marks must be explicit and replay-stable.
- Parsimony: no generic cycle engine, storage migration or automatic roll for a one-Put/one-Call V1.

## Findings

### PR-01 — [P1] Topology cannot be derived only from period facts and capital segments

- Status: accepted
- Current plan: S2 builds one-Put/one-Call topology from attributed facts/segments.
- Counterexample: an August residual-tail report contains no July open cash facts. It may contain only opening/ending valuation facts and a Call capital segment. A report-period fact reducer cannot prove which Funding Put belonged to the group, its immutable open lot identity, or its close transition.
- Direct evidence: `build_period_performance()` receives all scoped effective events and all scoped allocations, but `period_events`/`scoped_allocations` filter activity by report window. `OptionValuationPosition` represents only inventory at a valuation boundary.
- Impact: historical/current residual reports become topology-dependent on whether the open happened inside the requested period; identities and funding metrics would disappear across periods.
- Required fix: define a read-only group topology builder over all scoped effective events plus canonical allocations/projections. Period facts and capital segments are measurements attached to that topology, not the identity source.
- Re-review state: 已修复

### PR-02 — [P1] Funding attribution needs explicit lifetime-vs-period semantics

- Status: accepted
- Current plan: funding metrics are computed from canonical opening cash facts, but the plan does not say whether these are period activity or lifetime group facts.
- Counterexample: an August report for a July-opened group would either show no funding ratio or reintroduce July opening cash into August cash/PnL if the implementation tries to recover it from report facts.
- Direct evidence: current `PerformanceFact` generation includes open-event cash only when `period.contains(event.event_time_ms)`; canonical `events` remain available outside the period.
- Impact: funding coverage can become unstable by requested report period or contaminate period totals.
- Required fix: label funding as a `group_lifetime_opening_snapshot` derived from canonical group open events. It is informational metadata, never added to period cash/PnL. Add `source_event_ids` and quality.
- Re-review state: 已修复

### PR-03 — [P1] Conservation proof is underspecified

- Status: accepted
- Current plan: grouped sums equal the same selected fact set, but it does not define which facts enter PnL conservation or how CNY translation and missing net facts are handled.
- Counterexample: summing cash facts and realized facts together double counts the same trade economics; comparing CNY rounded values can produce noise; a missing fee makes realized net absent while realized gross remains available.
- Direct evidence: `PerformanceFact` includes activity cash, realized, and valuation kinds; top-level `_summarize()` exposes separate namespaces and quality envelopes.
- Impact: a green conservation flag may prove the wrong quantity or fail for valid partial evidence.
- Required fix: define conservation independently per canonical PnL metric (`realized_gross`, `realized_net`, opening/ending unrealized, `period_total_gross`, `period_total_net`) and native currency. Compare Decimal source-fact sums before serialization. Missing/partial metrics produce unavailable conservation, not zero. Cash/funding snapshots are excluded from PnL conservation.
- Re-review state: 已修复

### PR-04 — [P2] Strategy metadata conflict policy must identify the canonical owner

- Status: accepted
- Current plan: strategy metadata is extracted from `strategy_snapshot` with top-level fallback, but conflict precedence is not explicit.
- Counterexample: adjusted/imported legacy events can have a top-level `strategy_group_id` that disagrees with `strategy_snapshot.strategy_group_id`.
- Direct evidence: current resolver writes both top-level fields and `strategy_snapshot`; `_strategy_value()` in ledger economics prefers snapshot then top level.
- Impact: different performance paths could assign the same lot to different groups.
- Required fix: reuse the existing ledger precedence contract (snapshot first, top-level fallback). A non-empty conflict must mark attribution partial and keep canonical totals untouched; it must not silently select a group for management attribution.
- Re-review state: 已修复

### PR-05 — [P2] Assigned-stock continuation is broader than the proven V1 identity path

- Status: accepted
- Current plan: strategy group sums include assigned-stock continuation whenever projection fields carry provenance.
- Counterexample: historical assignment rows may carry a group on the option event but not on all derived stock sale/valuation facts; heuristic covered-call linkage is already allowed with downgraded lifecycle quality.
- Direct evidence: Option Performance design explicitly distinguishes explicit and heuristic assigned-stock/covered-call attribution and fails closed for mixed inventory.
- Impact: group PnL conservation could silently omit stock movement or overclaim complete lifecycle economics.
- Required fix: include assigned-stock group economics only under explicit canonical `strategy_group_id` provenance across the relevant projection facts. Otherwise expose `assigned_stock_attribution_unavailable` and keep group quality partial. Do not add a heuristic in this work unit.
- Re-review state: 已修复

### PR-06 — [P2] Public payload size and compatibility need a rows-off contract

- Status: accepted
- Current plan: additive `attribution.groups[]` is always present, but does not define behavior for `include_rows=false` or untagged portfolios.
- Counterexample: Agent consumers request summaries without fact rows; attribution must not depend on serializing rows, and empty portfolios should not become partial solely because no strategy attribution exists.
- Direct evidence: `PeriodPerformance.to_dict(include_rows=...)` conditionally omits rows while all summaries remain available.
- Impact: hidden dependency on rows or noisy quality regression for ordinary Sell Put/Covered Call reports.
- Required fix: compute attribution before serialization; `include_rows` changes only raw rows. No attributable Combo Yield groups yields observed empty attribution when source scope is proven, not a top-level quality downgrade. Malformed tagged groups downgrade attribution quality only unless the underlying canonical performance evidence is itself partial.
- Re-review state: 已修复

## Residual risks

| Risk | Classification |
|---|---|
| Exact Call PnL split at an intra-period Put close requires a transition mark | assigned to later evidence-capture work unit; V1 fail closed |
| Multiple Funding Put rolls against one Call | assigned to later work unit; identity format remains extensible |
| Broker margin differs from cash-secured notional | assigned to later work unit |
| Legacy groups without complete strategy metadata | explicit attribution partial; data repair owner later |
| Existing dirty scanning/runtime decoupling changes share the branch | preserved and staged separately; no implementation overlap unless proven |

## Verdict

The accounting direction is sound, but the plan is not implementation-ready until PR-01 through PR-06 are incorporated and re-reviewed. No architecture rewrite is required; all findings can be fixed by tightening identity sources, lifetime/period labels, conservation scope and quality contracts.


## Re-review

- Re-review date: 2026-07-18
- Reviewed fix: `Planreview Fix Addendum` in `docs/gateflow/cross-expiry-yield-capital-attribution-plan-20260718.md`
- PR-01: 已修复 — topology now comes from all scoped canonical events/allocations.
- PR-02: 已修复 — funding is explicitly a lifetime opening snapshot outside period totals.
- PR-03: 已修复 — conservation is metric-specific, native-currency and Decimal-based.
- PR-04: 已修复 — snapshot precedence and conflict fail-closed behavior are explicit.
- PR-05: 已修复 — assigned stock requires explicit canonical group provenance.
- PR-06: 已修复 — rows-off parity and observed-empty attribution are explicit.
- Blocking open questions: none.
- Final verdict: planreview pass.
- Completion status: complete.
