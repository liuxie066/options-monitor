# Gateflow Plan — 跨期收益和资金占用归因

- Gate: plan
- Work unit: Combo Yield staggered/diagonal 跨期收益与资金占用归因
- Date: 2026-07-18
- Goal artifact: `docs/gateflow/cross-expiry-yield-capital-attribution-goal-confirmation-20260718.md`
- Status: proposed; pending planreview
- Artifact path: `docs/gateflow/cross-expiry-yield-capital-attribution-plan-20260718.md`

## Accepted business semantics

1. Call premium 是 Participation Call 的成本基础，不作为首个 Funding Put 周期的一次性损失。
2. Put premium 对 Call 的覆盖是 funding attribution，不是额外 PnL 扣减。
3. 资本效率主口径是风险资本日：Short Put strike notional、Long Call premium basis、assigned-stock remaining cost basis。
4. 本轮支持一 Put + 一 Call + residual-call tail；为未来多个 Funding Put cycles 保留稳定 identity，但不实现 roll。

## Design decision

### Separate economic ownership from lifecycle labels

Canonical Option Performance totals remain unchanged. The new surface is additive and derives attribution from canonical facts:

- `funding_cycle` owns only Funding Put economics and Put capital exposure;
- `participation_lifecycle` owns only Participation Call economics and Call capital exposure;
- `strategy_group` is the sum of all attributable legs and assigned-stock continuation;
- `residual_call_tail` is a lifecycle/capital interval after Funding Put termination. It receives Call PnL only when the report has sufficient valuation boundaries to isolate that interval; otherwise its PnL/efficiency is explicitly unavailable.

This avoids inventing an intra-period Call mark at the Put close timestamp. Current Option Performance evidence guarantees report-opening and report-ending marks, not a mark at every strategy transition. V1 therefore must not split Call price movement across funding-cycle and residual-tail boundaries by interpolation.

### Stable identity

For current one-Put/one-Call groups:

```text
strategy_group_id = canonical existing group id
funding_cycle_id = "funding_cycle:" + funding_put_lot_id
participation_lifecycle_id = "participation:" + participation_call_lot_id
residual_tail_id = "residual_tail:" + strategy_group_id + ":" + funding_put_close_event_id
```

Identity is derived from immutable canonical event/lot IDs, not expiration. Future roll support can add more funding cycles without changing historical IDs.

### Funding metrics

Funding attribution is computed from canonical opening cash facts, not realized PnL:

```text
put_open_credit_gross
call_open_debit_gross
call_cost_funded_by_put = min(put_open_credit_gross, call_open_debit_gross)
funding_surplus = put_open_credit_gross - call_open_debit_gross
funding_ratio = put_open_credit_gross / call_open_debit_gross
```

Fees remain separate facts under existing fee-quality semantics. V1 exposes gross funding attribution plus fee evidence; it does not silently manufacture a net funding metric when actual fee provenance is missing.

### Capital metrics

Keep `notional_days_v1` unchanged. Add dimensions to capital segments and reduce them without altering top-level totals:

- strategy/group/leg/cycle identity;
- exact overlap interval;
- `capital_days_by_currency`;
- `average_incremental_capital_by_currency = capital_days / report_duration_days`;
- annualized efficiency only where PnL owner and capital owner are identical and both are complete.

Scope-safe efficiency:

- Funding cycle: Funding Put PnL / Funding Put capital-days.
- Participation lifecycle: Participation Call PnL / Participation Call capital-days.
- Strategy group: all attributable group PnL / all group incremental capital-days.
- Residual tail: Call PnL / Call tail capital-days only when report boundaries isolate the tail; otherwise status `not_observed`/`partial` with reason.

## Public contract

Add an additive top-level object without changing existing keys or semantics:

```text
attribution:
  schema_version: option_strategy_attribution.v1
  groups: [...]
  coverage: {...}
  conservation: {...}
```

Each group contains:

```text
strategy_group_id
strategy
structure
funding_cycles[]
participation_lifecycles[]
residual_tails[]
funding
pnl
capital
quality
```

`conservation` proves that attributed monetary facts are a subset of canonical facts and that grouped sums equal the same selected fact set. Unattributed or malformed strategy facts remain in top-level totals and are listed in coverage; they are never silently dropped or forced into a group.

## Implementation slices

### S1 — Canonical attribution provenance

Ownership:

- `domain/domain/performance/models.py`
- `domain/domain/performance/engine.py`
- `src/application/performance/adapters.py`
- focused performance model/engine tests

Changes:

1. Introduce a narrow immutable attribution provenance value (strategy, leg role, strategy group ID, optional lifecycle ID).
2. Extract strategy metadata from canonical open-event `strategy_snapshot` with top-level fallback, preserving current legacy behavior.
3. Carry provenance on `OptionValuationPosition`, `PerformanceFact`, and `CapitalExposureSegment` without changing monetary identity or amount calculations.
4. For close allocations, consume existing `OptionEconomicAllocation.strategy/leg_role/strategy_group_id`; do not rematch lots.
5. Carry assigned-stock group provenance only from canonical assigned-stock projection fields; missing linkage remains explicit.
6. Derive stable funding/participation IDs only when group structure proves exactly one Funding Put and one Participation Call for this V1 scope.

Validation:

- existing performance tests unchanged;
- open/close/unrealized/capital facts preserve provenance;
- malformed, missing, conflicting or non-Combo metadata fail closed for attribution while canonical totals remain observed as before;
- partial closes preserve the same lifecycle identity.

Residual risk destination:

- legacy untagged rows remain top-level-only and partial in attribution: current slice behavior, data repair later.

### S2 — Attribution reducer and conservation

Ownership:

- new narrow domain module under `domain/domain/performance/` if separation reduces engine size, otherwise focused helpers in `engine.py`;
- `domain/domain/performance/engine.py` integration;
- focused attribution tests.

Changes:

1. Build one-Put/one-Call group topology from canonical attributed facts/segments.
2. Produce separate Funding Put cycle, Participation Call lifecycle and strategy-group PnL/capital reducers.
3. Compute gross funding attribution from opening cash facts with explicit fee-quality evidence.
4. Emit average capital and scope-safe annualized efficiency.
5. Emit residual-tail capital intervals after the Funding Put close boundary.
6. Only emit residual-tail PnL when the report valuation window is wholly inside the tail or exact transition valuation evidence exists; V1 does not interpolate.
7. Add conservation evidence comparing attributed fact IDs/amounts with canonical selected facts.

Validation scenarios:

- Put and Call open together; Put expires in month 1; Call remains open through month 2; Call closes in month 3;
- report entirely within active-combo phase;
- report entirely within residual tail;
- report crosses Put close boundary without an exact Call mark: residual-tail PnL unavailable, group and leg totals still correct;
- same-month Put and Call closure;
- partial Put/Call closes;
- Put assignment and assigned-stock continuation;
- missing fees, FX, Call valuation, strategy metadata or malformed group;
- top-level totals byte-for-byte/equality compatible apart from additive attribution key.

Residual risk destination:

- exact intra-period tail split without persisted transition mark: assigned to later evidence-capture work unit unless a concrete reporting requirement justifies it.

### S3 — Application/public contract and docs

Ownership:

- `src/application/performance/service.py` only if adapter changes are needed;
- Agent/CLI contract tests for additive payload;
- `docs/OPTION_PERFORMANCE_DESIGN.md`;
- `docs/STRATEGY_ARCHITECTURE.md`;
- migration/compatibility docs if public snapshots require it.

Changes:

1. Surface additive attribution payload through existing `option_performance_report`; no new write tool or alternate report engine.
2. Preserve portfolio PnL/cash bridge inputs; bridges continue reading canonical top-level totals, not management attribution.
3. Document the three-ledger distinction: cash timing, economic PnL timing, management attribution.
4. Document funding metrics, capital formulas, conservation and unavailable semantics.
5. Add public contract tests proving old consumers remain valid and the new surface is deterministic.

Validation:

- focused Agent/CLI/Assistant contract tests;
- portfolio bridge regression tests;
- docs references and dependency boundaries;
- no generated config or production runtime mutation.

## Sequencing and commits

1. Accept reviewed plan commit.
2. S1 implementation -> deepreview -> fix/re-review -> accepted S1 commit.
3. S2 implementation -> deepreview -> fix/re-review -> accepted S2 commit.
4. S3 implementation -> deepreview -> fix/re-review -> accepted S3 commit.
5. Aggregate deepreview and accepted deepreview commit.
6. Ready-to-open-draft-PR checks, push, draft PR handling, PR deepreview/fix/re-review, final push, final closeout.

## Test strategy

Focused first:

```bash
python3 -m pytest tests/test_performance_models.py tests/test_performance_engine.py
python3 -m pytest tests/test_performance_service.py tests/test_option_performance_agent_tool.py
python3 -m pytest tests/test_portfolio_agent_tool.py tests/test_portfolio_pnl_bridge.py
```

Discover exact existing filenames before execution; do not invent missing paths. Then broader risk gate:

```bash
python3 -m pytest tests/test_ledger_*.py tests/test_option_positions_*.py tests/test_combo_yield_*.py
python3 -m ruff check <changed production and test files>
git diff --check
```

Run the repository analyze/full relevant baseline when resource budget permits and before PR pass.

## Docs decision

Docs update is required because this adds a public additive payload and defines strategy/accounting semantics. The authoritative contracts are `docs/OPTION_PERFORMANCE_DESIGN.md` and `docs/STRATEGY_ARCHITECTURE.md`.

## Explicit non-goals

- no production config edits;
- no notifications or state writes;
- no automatic roll/new Funding Put;
- no broker-margin/NAV return;
- no tax accounting;
- no interpolation of missing option marks;
- no replacement of canonical top-level performance totals with management attribution;
- no refactor of current scanning/runtime-decoupling dirty files unless a reviewed dependency is proven.

## Plan completion criteria

- all findings from planreview are adjudicated;
- accepted findings are fixed and re-reviewed;
- no blocking open question;
- all residual risks have an owner/destination;
- plan is code-generation-ready without implementation-time product decisions.

## Planreview Fix Addendum

This addendum is normative and supersedes any less-specific wording above.

### Identity/topology source

The group topology builder consumes **all scoped effective canonical trade events and canonical allocations**, not only facts selected into the requested report period. It must:

1. resolve each open lot from its immutable open event/lot ID;
2. apply the existing snapshot-first/top-level-fallback strategy metadata precedence;
3. reject non-empty metadata conflicts from management attribution while leaving canonical accounting unchanged;
4. prove exactly one `funding_put` open lot and one `participation_call` open lot for V1;
5. derive transition times from canonical close allocations, including partial/full close state;
6. attach report-period facts and capital segments to this stable topology after identity is established.

`funding_cycle_id`, `participation_lifecycle_id`, and tail identity remain stable when the report starts after original opens.

### Funding lifetime snapshot

`funding` is explicitly:

```text
scope = group_lifetime_opening_snapshot
```

It is derived from the canonical group open events even when those opens are outside the report period. It includes `source_event_ids`, native currency, gross credit/debit, fee evidence, funding ratio/surplus and quality. It is informational and is never inserted into period `cash`, `pnl`, or their conservation totals.

### Metric-specific conservation

Conservation is computed in native currency from Decimal source facts before serialization and independently for:

- `realized_gross`;
- `realized_net`;
- `opening_unrealized_gross` / `opening_unrealized_net`;
- `ending_unrealized_gross` / `ending_unrealized_net`;
- `period_total_gross`;
- `period_total_net`.

Each envelope records selected canonical fact IDs, attributed fact IDs, unattributed tagged fact IDs, source amount, grouped amount, residual and status. Cash/activity/funding snapshot facts are excluded from PnL conservation. A missing or partial canonical metric produces unavailable/partial conservation rather than a synthetic zero.

### Metadata conflict and quality

- Existing ledger precedence remains `strategy_snapshot` first, top-level fallback.
- If both are non-empty and disagree, the fact/segment is not assigned to a management group and attribution coverage records `strategy_metadata_conflict:<event_id>`.
- Canonical total quality is not downgraded solely by management-attribution failure.
- Attribution has its own quality envelope.

### Assigned-stock boundary

Assigned-stock continuation enters group attribution only when the canonical assignment/stock projection exposes an explicit, consistent `strategy_group_id` for the relevant lot and derived facts. Heuristic stock or covered-call attribution is not promoted into Combo Yield group economics. Missing explicit provenance yields `assigned_stock_attribution_unavailable:<source_id>` in attribution quality while canonical stock PnL/capital remains unchanged.

### Serialization and empty state

Attribution is computed independently of `include_rows`. `include_rows=false` omits only raw `rows`; group summaries and conservation remain identical. A proven scope with no attributable Combo Yield group emits an observed empty attribution object and does not downgrade top-level quality. Malformed tagged groups downgrade only attribution quality unless the same underlying evidence already affects canonical performance quality.

### Revised slice details

- S1 also implements the canonical all-event topology/provenance extraction contract and metadata conflict diagnostics.
- S2 consumes that topology, emits the lifetime funding snapshot, metric-specific conservation, and attribution-owned quality.
- S3 verifies `include_rows` parity, observed-empty semantics and unchanged portfolio bridge behavior.
