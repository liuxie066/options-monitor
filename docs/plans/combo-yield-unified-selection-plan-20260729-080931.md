# Combo Yield 同期与跨期统一筛选排序计划

- Date: 2026-07-29
- Status: proposed; pending planreview
- Scope: Combo Yield opening candidate filtering, ranking, evidence, and shadow promotion
- Artifact: `docs/plans/combo-yield-unified-selection-plan-20260729-080931.md`

## 1. Goal

在不改变 standalone Sell Put、canonical ledger、Option Performance 和真实成交语义的前提下，统一
`same_expiry_pair` 与 `staggered_expiry_pair` 的候选处理骨架，移除正式排序对
`premium_funding_score` 的依赖，并使每个正式排序参数只承担一个可解释职责：

1. Funding Put 由 Combo Yield 自己构造的完整 underwriting universe 决定；
2. Call 成本由净权利金保留率硬门槛控制；
3. Call 参与质量由期限结构适用的目标参数决定；
4. 流动性只做硬门槛和稳定破同分；
5. 同期与跨期共享事实字段和选择阶段，但不共享错误的单期限收益指标。

## 2. Non-goals

- 不修改 standalone Sell Put 的候选、排序、通知或成功状态。
- 不让 Combo Yield 依赖 standalone Sell Put step 是否启用或是否产出候选。
- 不修改生产 `config.yaml`、`config.us.json`、`config.hk.json`。
- 不自动开仓、配对、roll、关闭或写入 ledger。
- 不改变既有 `strategy_group_id`、`pair_intent_id`、Performance attribution 或 residual-call
  lifecycle identity。
- 不在本 work unit 定义 portfolio-level 同期/跨期资本占用上限。
- 不把跨期结构晋升为正式推荐；本轮只使其可做确定性 Shadow 比较。
- 不删除 legacy `premium_funding_score` 字段或历史 artifact 兼容读取。

## 3. Accepted strategy semantics

### 3.1 Shared relationship

两种结构均为严格一对一：

```text
1 Short Put funding leg : 1 Long Call participation leg
same canonical symbol
same currency
same multiplier
put strike < call strike
```

Funding Put 必须先通过 Combo Yield 自己的完整 Put scan、事件、现金和
`insurance_underwriting`。Call 不得放宽 Put 的接货边界。

### 3.2 Same expiry

```text
put.expiration == call.expiration
call strike >= spot
```

允许计算同到期 breakeven、expected move、1.5σ/2.0σ scenario 和组合年化净收入率。

### 3.3 Staggered expiry

```text
put.expiration < call.expiration
put strike < call strike
```

不计算或硬筛单一组合年化、同到期 breakeven 或同到期 payoff multiple。Call 在 Put
到期后的 residual tail 继续由既有 group/lifecycle attribution 追踪。

## 4. Unified pipeline

```text
required-data completeness
  -> Combo-owned Funding Put scan
  -> event/cash/underwriting hard gates
  -> Funding Put canonical underwriting rank
  -> structure-specific Call universe
  -> pair structure validation
  -> execution-price funding metrics
  -> structure-specific hard gates
  -> choose one Call per Funding Put
  -> choose one pair per symbol
  -> baseline/proposed Shadow comparison
  -> candidate/trace/diagnostic/brief surfaces
```

Required-data planning remains globally fail-closed. A partial expiration universe must not emit a
claim of globally optimal Call selection.

## 5. Funding Put rank contract

Combo Yield does not consume standalone Sell Put output. After
`enrich_and_filter_sell_put_underwriting()` returns the Combo-owned accepted Put universe, assign:

```text
funding_put_rank = one-based position in rank_underwriting_candidates(..., mode="put")
funding_put_rank_scope = symbol + account + run_id + quote_snapshot_id + policy hash
```

The rank is run-scoped evidence, not a durable contract identity. Pair rows preserve the original
Put contract, rank, rank drivers and source evidence. Pair filtering may eliminate a higher-ranked
Put when it has no eligible Call; remaining pairs retain the original rank rather than being
renumbered.

The underwriting rank semantics remain:

1. Put annualized return descending;
2. net assignment discount descending;
3. concentration tie-break;
4. spread ascending;
5. OI and net income tie-breaks.

`strategy_score` and `premium_edge_score` remain diagnostic-only and are not substituted for the
canonical underwriting rank tuple.

## 6. Shared funding hard gates

Use executable-side estimates:

```text
put_net_credit = put_bid * multiplier - estimated_sell_fees
call_total_cost = call_ask * multiplier + estimated_buy_fees
combo_net_credit = put_net_credit - call_total_cost
net_credit_retention = combo_net_credit / put_net_credit
```

Shared hard gates:

- positive Put credit and Call cost;
- `funding_mode=credit_or_even`;
- `combo_net_credit >= 0`;
- leg liquidity and execution fields available;
- `min_net_credit_retention` satisfied;
- structure relation valid.

`max_call_cost_to_put_credit` is the algebraic complement of retention and must not participate in
new ranking. Keep it as a compatibility input; reject conflicting explicit values during validation
or define one documented precedence before implementation.

Research hypotheses, not production defaults:

| Structure | Retention variant | Meaning |
|---|---:|---|
| same expiry | `0.75` | Call uses at most 25% of Put net credit |
| staggered expiry | `0.80` | longer-lived Call uses at most 20% of Put net credit |

Same expiry retains the existing `min_net_credit_annualized` hard gate during baseline comparison.
Staggered expiry keeps this field unavailable.

## 7. Call quality policy

Shared research Delta band:

```text
min_abs_call_delta = 0.10
target_abs_call_delta variants = 0.20 and 0.25
max_abs_call_delta = 0.30
```

The target is a research variant. It is not added to production authoring config until Shadow
evidence selects a variant and the promotion is separately approved.

### 7.1 Same-expiry Call selection

Within one Funding Put, after hard gates:

```text
abs(abs(call_delta) - target_abs_call_delta) ascending
call_spread_ratio ascending
call_open_interest descending
call_contract_symbol ascending
```

`call_payoff_multiple_at_1_5_sigma`, `call_payoff_multiple_at_2_0_sigma`, scenario scores and
matched-delta IV skew remain research/explanation fields. They do not enter V1 formal ranking.

### 7.2 Staggered-expiry Call selection

Relative expiry parameters are research variants:

```text
min_expiry_gap_days = 14
target_expiry_gap_days = 28
max_expiry_gap_days = 45
```

Within one Funding Put, after hard gates:

```text
abs(expiry_gap_days - target_expiry_gap_days) ascending
abs(abs(call_delta) - target_abs_call_delta) ascending
call_spread_ratio ascending
call_open_interest descending
call_contract_symbol ascending
```

These values describe a candidate residual horizon, not a proven optimum. Existing wider production
compatibility ranges remain untouched until promotion evidence exists.

Entry Delta and expiry gap are selection controls, not claims about Call value at Put expiry.
Term IV, Theta and projected residual Call value remain research fields until exact synchronized
mark paths support them.

## 8. Pair selection

After selecting at most one Call per Funding Put, choose one pair per symbol:

```text
funding_put_rank ascending
structure-specific Call rank tuple
put_contract_symbol ascending
call_contract_symbol ascending
```

This deliberately makes Funding Put quality dominant. A better Call cannot cause a materially
lower-ranked Put to replace a higher-ranked Put; if the higher-ranked Put has no eligible Call, the
next eligible Funding Put may win.

Same- and staggered-expiry rows are never mixed in one symbol-level rank operation. A run evaluates
the configured `structure_mode`; research compares variants in separate labeled result sets.

## 9. `premium_funding_score` compatibility

- Continue computing and serializing the field for historical compatibility.
- Add explicit metadata/contract documentation that it is `diagnostic_only`.
- Remove it from `yield_enhancement_rank_key()` for same-expiry candidate selection.
- Ensure candidate file order, per-Put Call selection, per-symbol selection, report summary and alert
  rendering all call the same new domain ranking authority.
- Do not delete or reinterpret historical values in this work unit.

## 10. Evidence and output contract

Every accepted and rejected pair diagnostic must include:

- structure mode and policy variant ID;
- Funding Put contract, original rank and rank scope evidence;
- Call contract, Delta target distance and expiry-gap target distance where applicable;
- executable Put credit, Call cost, combo net credit and retention;
- hard-gate reasons;
- baseline rank/selected flag;
- proposed rank/selected flag;
- deterministic rank-change reason.

The canonical candidate artifact keeps one selected pair per symbol. Shadow variant artifacts never
change the live selection or notification.

## 11. Cross-expiry lifecycle and promotion boundary

The existing one-Put/one-Call group and residual-tail attribution remains authoritative. This plan
does not rematch actual positions from scan candidates.

Cross-expiry cannot be promoted from Shadow until a separate reviewed control proves:

- how active staggered groups and residual Calls count against portfolio/symbol exposure;
- whether another Funding Put may open while a residual Call remains;
- how assignment-created stock plus Long Call affects the opening cap;
- how partial closes and missing marks fail closed;
- no automatic roll or heuristic rebinding.

Absent that control, staggered rows remain advisory/research even when candidate ranking succeeds.

## 12. Validation plan

### 12.1 Deterministic domain tests

- retention and call-cost complement, including fees and boundary equality;
- missing Delta, bid/ask, multiplier, IV and liquidity fail closed as specified;
- original Funding Put rank survives pair filtering without renumbering;
- same-expiry target-Delta selection;
- staggered target-gap then target-Delta selection;
- stable lexical tie-breaks;
- same/staggered rows cannot be mixed;
- legacy `premium_funding_score` changes do not alter proposed ranking;
- baseline ranking remains reproducible in Shadow artifact.

### 12.2 Application/contract tests

- Combo remains independent of standalone Sell Put enablement and candidate outputs;
- required-data planner covers all expirations needed by every variant before scan;
- pair diagnostics, candidate CSV, summary, alert and Daily Brief agree on the selected pair;
- empty and all-rejected universes clear stale artifacts;
- generated config validation rejects unsupported/conflicting combinations;
- no notification or ledger write occurs in Shadow evaluation.

### 12.3 Replay and outcome evidence

Compare labeled variants:

1. current baseline;
2. same expiry retention 75%, target Delta 20;
3. same expiry retention 75%, target Delta 25;
4. staggered retention 80%, target gap 28, target Delta 20;
5. staggered retention 80%, target gap 28, target Delta 25.

Candidate replay measures coverage, rejection distribution, rank changes, cost/retention and
liquidity. It must not claim PnL superiority.

Outcome evaluation is group-level and separately reports Funding Put, Participation Call,
strategy-group and residual-tail evidence. Missing synchronized marks remain unavailable rather
than imputed. Promotion requires completed multi-horizon outcome evidence and human review; this
plan intentionally does not invent a sample-size or superiority threshold without observed data.

## 13. Implementation slices

### S1 — Rank contract and baseline-preserving evidence

- Add run-scoped Funding Put rank/provenance after Combo-owned underwriting.
- Centralize structure-specific proposed rank keys in
  `domain/domain/engine/yield_enhancement.py`.
- Extend rank-shadow artifacts with variant IDs and reason fields.
- Preserve production selection unchanged.

### S2 — Same-expiry Shadow policy

- Add retention 75% and Delta 20/25 variants to research evaluation, not production config.
- Prove candidate/summary/alert selection parity for each variant.
- Keep scenario and `premium_funding_score` diagnostic-only in proposed results.

### S3 — Staggered-expiry Shadow policy

- Add retention 80%, relative-gap and Delta variants to research evaluation.
- Preserve unavailable single-horizon metrics.
- Connect selected research rows to existing group-level evaluator only when canonical group
  identity and outcome evidence exist.

### S4 — Promotion decision, separate authorization

- Review replay/outcome evidence.
- Resolve portfolio/symbol staggered exposure controls.
- Select or reject parameter variants.
- Only after explicit approval: update code defaults/config schema/examples/docs and switch formal
  rank authority.

## 14. Success criteria

- One domain authority deterministically ranks each structure.
- No duplicated cost/retention terms in proposed ranking.
- Combo-owned Funding Put ranking remains independent of standalone Sell Put runtime success.
- Same-expiry and staggered semantics cannot leak into each other.
- Every rank change is explainable from persisted evidence.
- Shadow variants cannot alter production candidates, notifications, config or ledger.
- Cross-expiry remains non-promotable until exposure/lifecycle opening controls are reviewed.

## 15. Residual risks

- Delta 20/25, retention 75/80 and gap 14/28/45 are hypotheses, not proven optima.
- Ranking by underwriting return first may select a higher-premium Put over a safer lower-return Put;
  this is inherited canonical underwriting semantics and must be evaluated rather than silently
  redefined here.
- Single-stock skew and term structure are regime-dependent.
- Sparse or incomplete historical marks may prevent outcome comparison.
- Integer Funding Put rank is comparable only within its recorded run scope.

## 16. Planreview Fix Addendum

This addendum is normative and supersedes conflicting or less-specific wording in Sections 4-13.
It resolves PR-01 through PR-05 from
`docs/reviews/plan-review-20260729-080931.md`.

### 16.1 Baseline and proposed rank authorities are separate

S0-S3 are Shadow-only and must not edit the delegate used by production candidate selection,
summary, alert or Daily Brief.

Keep the current production authority unchanged:

```text
rank_yield_enhancement_rows()
rank_yield_enhancement_calls_for_put()
select_best_yield_enhancement_per_symbol()
```

Add separate research-only authorities with an explicit immutable policy input:

```text
rank_combo_yield_proposed_rows(rows, policy)
rank_combo_yield_proposed_calls_for_put(rows, policy)
select_best_combo_yield_proposed_pairs(rows, policy)
```

`policy` contains structure mode, retention hypothesis, Delta band/target, and staggered gap
band/target. It is created only by the Research experiment boundary in S1-S3. Runtime candidate
configuration cannot implicitly instantiate it.

Only the separately authorized S4 promotion work unit may change the production delegate. That work
unit must:

1. select one reviewed policy variant;
2. update production config schema/defaults/examples only under explicit config-change authority;
3. switch the production rank delegate in one narrow domain change;
4. prove candidate file, summary, alert and Daily Brief select the same row;
5. keep a rollback flag/delegate to the existing baseline for the first release;
6. not delete `premium_funding_score`.

Therefore Section 9's removal of `premium_funding_score` from formal ranking is an S4 action, not an
S1-S3 action.

### 16.2 Research-only required-data capture

Historical runs cannot evaluate expirations they did not fetch. S3 must not reuse an incomplete
production required-data universe.

Add one explicit Research CLI action under the existing Shadow Replay family:

```text
./om research shadow-replay capture-combo-variants \
  --config-key <market> \
  --account <account> \
  --symbols <symbols...> \
  --variant-spec <json-path> \
  [--write]
```

Safety and ownership:

- preview/dry-run by default;
- `--write` writes local research evidence and local quote/rate-limit cache only;
- no notification, ledger, position, config or production output write;
- no broker order or pair intent;
- output root is the existing local Shadow Replay dataset root;
- CLI adapter stays thin; planning/capture lives in `src/application/shadow_replay/`;
- reuse the existing domain/application candidate and required-data functions rather than fork
  pricing or filtering logic.

The action constructs two plans:

1. **production reference plan** from the effective runtime config, for comparison only;
2. **research supplement plan** from the exact variant spec.

The research supplement unions every Put and Call expiration required by all declared variants.
The physical research capture plan is the deduplicated union of the production reference plan and
that supplement, so baseline and variants are evaluated from the same captured bytes even when
their windows do not overlap. It runs in an isolated research root and never expands or blocks a
production tick. A production-reference planning failure and a supplemental planning/fetch failure
have independent receipts and completeness states.

The production reference plan is never a pre-filter for the research plan. The supplement first
constructs a structural superset of contracts and pairs that could pass **any** declared variant:

- expiration discovery uses the union of all variant windows;
- Delta, retention, Call-cost, liquidity and spread bounds use the least restrictive union needed
  to retain every variant's possible inputs;
- each variant then applies its own complete hard gates to that immutable superset;
- a stricter current production setting cannot erase candidates needed by a looser research
  variant;
- when production Combo is disabled, research may ignore only the `enabled` switch while preserving
  and hashing every other effective policy field; the manifest records this override and production
  remains disabled.

This is a capture/evaluation superset, not a recommendation universe. The required variant spec
contains a positive research-only `max_estimated_option_chain_calls`. Before any fetch, reuse
`build_prefetch_budget_plan()` to report waves, oversized symbols and total estimated calls. If the
total exceeds the authored research cap, preview returns the estimate and capture fails closed; it
does not silently truncate expirations, strikes or symbols. The runtime rate-limit configuration
still governs execution but is not misrepresented as a total-request cap.

The capture manifest must contain:

```text
schema_version
dataset_id
capture_observed_at_utc
market/account/symbols
normalized_effective_combo_policy
effective_combo_policy_hash
normalized_variant_spec
variant_spec_hash
planned_put_expirations
planned_call_expirations_by_variant
discovered_expirations
fetched_expirations
required_data_file_sha256
variant_completeness[]
safety
```

Each variant has status `complete` or `unavailable` with explicit missing expirations/contracts.
Incomplete variants emit no proposed winner and cannot enter outcome comparison. Source hashes are
computed from exact captured bytes before candidate construction.

Required validation:

- a same-expiry production config can capture later staggered research expirations;
- an existing staggered `30-90` range can capture a separate `14-45` research variant;
- a strict production Delta/retention policy cannot pre-filter candidates required by a looser
  research variant;
- stale or cross-time Put/Call/spot quotes fail pair completeness;
- incomplete supplemental fetch does not affect production and cannot claim a rank winner;
- repeated capture with the same immutable inputs is deterministic or fails on target collision; it
  never overwrites a completed dataset.

### 16.3 Counterfactual Combo pair dataset facet

Candidate rank replay alone cannot prove risk/return. Canonical
`strategy_group_id` is reserved for actual ledger groups and must not be invented for research
candidates.

Extend the Shadow Replay dataset with an optional Combo variant facet:

```text
combo_pair_decisions.jsonl
combo_pair_mark_paths.jsonl
combo_pair_outcomes.jsonl
```

The dataset manifest records the facet schema versions, exact file hashes and completeness.

Research identity:

```text
shadow_combo_pair_id = sha256(
  dataset_id,
  account,
  symbol,
  structure_mode,
  put_contract_symbol,
  call_contract_symbol,
  entry_observed_at_utc
)
```

It is not a `strategy_group_id`, `pair_intent_id` or trade identity.

The decision facet contains the complete hard-gate-passing pair universe from one captured entry
snapshot plus the baseline/proposed selected flags for every declared variant. All variants for one
decision instance use the same captured source bytes and fees model.

Entry evidence is usable only when the underlying, Put and Call observations each have a source
timestamp and satisfy explicit `max_entry_quote_age_seconds` and
`max_entry_leg_skew_seconds` values in the normalized variant spec. The manifest records observed
ages/skew and the applied limits. Missing timestamps, stale legs or excessive cross-leg skew make
that pair unavailable for every affected variant; file hashes alone are not evidence of a
contemporaneous executable pair.

Mark collection:

- collect marks for the union of every Put, Call and underlying referenced by any selected baseline
  or proposed pair;
- record bid/ask/mid, underlying spot, observation time, source and quality;
- require a Put-expiry settlement observation and, for staggered pairs, a Call-expiry settlement
  observation;
- collect intermediate underlying/option marks needed to measure path drawdown;
- OpenD collection cannot recover past marks; missing historical marks remain unavailable;
- missing quotes are evidence gaps, never zero or interpolated values.

Counterfactual economics use conservative executable sides:

```text
entry Put = sell at bid less estimated open fee
entry Call = buy at ask plus estimated open fee
early Put close = buy at ask plus estimated close fee
early Call close = sell at bid less estimated close fee
expiration = intrinsic settlement from authoritative expiry spot
```

At Put expiry:

- OTM Put terminates with no assigned stock;
- ITM Put creates a research-only assigned-stock continuation of
  `multiplier` shares at the strike;
- the staggered Call remains marked independently;
- the research state becomes `residual_call` or `assigned_stock_plus_residual_call`.

The research-only stock continuation is marked from Put settlement through Call settlement. It does
not enter the broker ledger. Missing underlying marks make post-assignment group outcome unavailable.
No covered-call, roll or discretionary stock-sale behavior is imputed.

For normalized research measurement, an expiring long Call is valued as a cash-equivalent intrinsic
payoff and does not create a second physical stock lot. Any stock created by Put assignment is
liquidated notionally at the authoritative Call-settlement spot when the full shadow group closes.
Fees and slippage for that notional liquidation are explicit model inputs. The outcome labels this
as a research settlement convention, not broker exercise simulation. A future physical-exercise
lifecycle model would require a separate reviewed work unit.

Counterfactual marks cannot prove whether an American-style short Put would have been assigned
early. The evaluator therefore never invents an early-assignment event. It labels the base outcome
`expiry_assignment_model` and also emits an early-assignment stress envelope at every observed mark
where the Put is ITM: assignment at strike on that observation, then stock/Call marking through Call
settlement under the same fee and capital conventions. This envelope is a scenario, not probability
weighted PnL. Missing intermediate observations make the stress envelope incomplete.

Outcome scopes:

1. `funding_horizon`: entry through Put settlement, with remaining Call and assigned stock marked;
2. `participation_horizon`: Call entry through Call settlement;
3. `full_shadow_group`: all option and research-assigned-stock economics through Call settlement;
4. `put_only_baseline`: the same Funding Put without the Call, using identical marks and fees.

Every outcome separates Put PnL, Call PnL, assigned-stock continuation, Call cost funded by Put, group
PnL, capital-days, maximum observed drawdown and evidence quality. Funding attribution is not double
counted as PnL.

Capital exposure follows the existing Performance attribution semantics without creating ledger
events:

- Funding Put capital-days use Put strike notional for its open interval;
- Participation Call capital-days use paid Call premium for its open interval;
- research-assigned stock capital-days use assigned-stock notional from Put settlement until the
  notional liquidation;
- group capital-days are the additive leg/stock components in native currency, with the same
  interval-boundary convention as `domain/domain/performance/strategy_attribution.py`;
- missing multiplier, interval boundary or currency makes capital efficiency unavailable.

The facet persists the components as well as the sum, so staggered residual exposure cannot be
hidden inside one annualized ratio. Any divergence from the canonical Performance formula is a
schema-versioned research metric and cannot be used for promotion.

Baseline and every proposed variant are compared only on identical decision instances for which all
required horizons are complete. Coverage loss is reported separately and cannot improve a score by
dropping difficult cases.

### 16.4 Rank provenance authority

Do not retrofit Position Advice's `quote_snapshot_id` or derive identity from artifact paths.

The Combo research capture manifest defined in 16.2 is the authority. Define:

```text
combo_rank_scope_hash = sha256(
  dataset_id,
  account,
  symbol,
  entry_observed_at_utc,
  effective_combo_policy_hash,
  variant_spec_hash,
  required_data_file_sha256
)
```

After Combo-owned underwriting, assign the one-based `funding_put_rank` and persist:

```text
funding_put_rank
funding_put_rank_key
combo_rank_scope_hash
source_candidate_count
```

`funding_put_rank_key` is the serialized canonical underwriting sort tuple with typed fields, not a
Python `repr`. Missing scope hash, source hashes or rank key makes the proposed experiment
unavailable. Pair filtering preserves the original rank without renumbering.

Production baseline artifacts do not need these new research provenance fields until S4 promotion.

### 16.5 Compatibility rule for retention and Call-cost ratio

The normative rule is:

- `min_net_credit_retention` and `max_call_cost_to_put_credit` remain independent explicit hard
  gates when both are authored;
- both must pass;
- diagnostics name the specific failed gate or both;
- new defaults and examples author retention only;
- no automatic conversion, precedence or silent dropping;
- equality passes using the existing numeric comparison semantics.

This is backward compatible and deterministic even when explicit values are not exact complements.
Deprecation/removal of `max_call_cost_to_put_credit` is outside this plan.

### 16.6 Promotion evidence and independent structure decisions

Same-expiry and staggered promotion decisions are independent.

Same expiry may enter S4 when:

- baseline and variants are evaluated on identical complete decision instances;
- candidate coverage, rejection shifts, group PnL, Put-only delta, maximum drawdown, Call loss rate,
  execution spread and missing-evidence rate are all reported;
- no data-quality or execution-risk regression is hidden by reduced coverage;
- a human-reviewed decision record selects a variant or rejects all variants.

Staggered expiry additionally requires:

- complete funding and participation horizons;
- expiry-assigned-stock/residual-Call cases and early-assignment stress envelopes represented or
  explicitly declared unavailable;
- either broker-observed early-assignment cases under the same lifecycle contract or a human
  decision that accepts the documented lack of empirical early-assignment frequency;
- the separately reviewed portfolio/symbol exposure control named in Section 11;
- no automatic roll/rebinding assumption.

No implementation agent chooses Delta, retention, gap or statistical superiority thresholds during
S1-S3. S4 begins only after the human decision record supplies those product choices. A variant can
be rejected without changing production.

### 16.7 Revised implementation sequence

#### S0 — Dataset facet and research capture design contract

- Finalize schemas for variant spec, capture manifest, decisions, marks and outcomes.
- Add CLI contract and dry-run/write safety tests.
- No rank behavior change.

#### S1 — Isolated research required-data capture

- Implement 16.2 in a local research output root.
- Prove variant completeness and immutable source hashes.
- No production candidate or notification change.

#### S2 — Separate proposed rank authorities

- Implement 16.1, 16.4 and 16.5.
- Produce baseline/proposed decision facets from the same captured universe.
- Current production delegate remains byte-for-byte behavior compatible.

#### S3 — Prospective marks and counterfactual outcomes

- Implement 16.3 for same-expiry first, then staggered/assigned-stock continuation as a separate
  sub-slice.
- Fail closed on missing marks.
- Emit structure-specific scorecards without proposals or config patches.

#### S4 — Human promotion work unit

- Requires a reviewed human decision record and, for staggered, exposure-control readiness.
- May promote same expiry while staggered remains Shadow.
- Requires separate planreview and explicit production config authority.

### 16.8 Revised completion criteria

S0-S3 are implementation-ready only when:

- research capture can supply the complete declared variant universe without touching production
  outputs;
- baseline and proposed rank authorities are separate;
- counterfactual pair outcomes use research identity and identical decision instances;
- rank provenance is hash-bound to exact captured bytes and policy;
- explicit retention/Call-cost gates have deterministic semantics;
- no S0-S3 path can send notifications, write ledger/config, or change production candidate order.

S4 is not part of the current implementation authorization and cannot be inferred from successful
Shadow evidence.
