# Gateflow Plan — Close Advice Strategy Optimization

## 1. Work unit

- **Work unit**: `close-advice-strategy-optimization`
- **Gate**: `plan`
- **Plan date**: 2026-07-23
- **Repository baseline**: `origin/main@64ccd3e0`
- **Scope owner**: Close Advice strategy policy and its offline evidence lifecycle
- **Plan status**: accepted after plan re-review; next entry point S1
- **User boundary**: implementation authorized; S5 remains a mandatory evidence/CEO stop gate before production promotion

This plan follows the current repository architecture and preserves the rule that strategy research is offline/advisory until explicit CEO promotion approval.

## 2. Goal, motivation, and success signals

### 2.1 Goal

Replace the current one-size-fits-all Close Advice action rule with one profile-aware decision contract that:

1. separates economic opportunity, strategy-thesis evidence, and the user-facing recommendation;
2. prevents a medium threshold hit such as `remaining_annualized_return <= 7%` from becoming an unconditional “建议平仓” for an `insurance_underwriting` lot;
3. preserves a distinct `return_first` exit policy rather than forcing underwriting semantics onto every historical lot;
4. uses transaction cost, remaining risk, continued willingness, and feasible replacement evidence when those facts are decision-relevant;
5. proves any policy change through point-in-time Shadow Replay before changing the production path;
6. keeps Close Advice advisory-only and never writes orders, broker state, position state, ledger events, or runtime config.

### 2.2 Motivation

The current production policy has a semantic mismatch:

- opening config resolves Sell Put and Covered Call to `insurance_underwriting` and requires net return, IV/RV edge, and event-risk evidence;
- closing resolves the lot profile correctly, but the short-vol wrapper preserves the same return-capture tier as the legacy path;
- `medium` is currently `DTE >= 14`, `capture_ratio >= 70%`, and remaining annualized return `<= 7%`;
- `tier=medium` maps to `exit_state=profit_capture`, then to `close_action=close`, and the Daily Brief renders that action as “建议平仓”;
- IV/RV, delta, event, stress, continued willingness, and replacement evidence are additive observations or shadow outputs and do not control the formal action.

The resulting recommendation is internally consistent with the legacy threshold, but it is not yet a complete underwriting decision.

### 2.3 Success signals

The work unit is successful only when all of the following are true:

- A deterministic policy truth table exists for `return_first`, `insurance_underwriting`, Yield Enhancement short-put legs, and the existing long-call convexity path.
- A medium profit-capture signal alone never produces an unconditional production close action for an underwriting lot.
- The system distinguishes `hold`, `review`, `close`, and `not_evaluable` without inferring an order or broker action.
- `tier` is urgency/evidence strength, not the sole notification/action authority.
- Point-in-time replay compares current policy and proposed policies using only facts available at each decision timestamp.
- Replay includes settled lifecycle outcomes and rejected/held counterfactual evidence; it does not judge policy only from previously notified rows.
- A promotion report quantifies premature-close regret, avoided losses, transaction costs, tail/path risk, assignment/called-away alignment, notification precision, and repeated reminders.
- No policy is promoted with insufficient outcome coverage or an inconclusive segment.
- Exactly one production policy remains authoritative after promotion; shadow variants are not runtime strategies.
- Current Daily Brief structure, Feishu paragraph rendering, candidate ranking, ledger authority, and automatic-trading boundaries remain intact.

## 3. First-principles judgment and direct evidence

### 3.1 The strategy problem is valid

For a short Put:

```text
remaining_annualized_return
= close_mid / strike * 365 / DTE
```

At strike `100`, DTE `30`, premium `2.00`, and mid approximately `0.575`, the lot has:

```text
capture_ratio ~= 71.2%
remaining_annualized_return ~= 7.0%
```

The current medium rule therefore recommends closing. This is exact current behavior, not a rendering bug.

### 3.2 The metric is useful but insufficient

`remaining_annualized_return` measures gross remaining option premium relative to strike for Put or spot for Call. It is useful as one reward-density fact, but it is not:

- expected return;
- fee-adjusted replacement advantage;
- risk-adjusted return;
- assignment/called-away utility;
- a complete comparison of close-now, hold, and switch.

Therefore the field must remain an input fact and must not remain the sole medium-action authority.

### 3.3 Existing architecture already has most required facts

Current code already exposes:

- strategy resolution from lot snapshot with config fallback;
- quote quality, spread, capture ratio, DTE, remaining premium, and remaining annualized return;
- fee status, fee basis, buy-to-close cost, and net close P&L;
- IV/RV, delta, event context, and remaining stress observations;
- explicit replacement annualized return when supplied;
- `assignment_acceptable` / `called_away_acceptable` and calibration completeness;
- a capital-reallocation shadow with close/open fees, slippage, capacity, daily-yield advantage, and recovery horizon;
- Shadow Replay capture, marks, settlement, readiness, parameter sets, and analysis.

The primary architecture gap is not missing data classes or missing infrastructure. It is that these facts terminate in diagnostics/shadow outputs while formal action remains tier-driven.

### 3.4 Why a direct threshold edit is rejected

Changing `7%` to `4.5%`, `5%`, or another number would reduce reminders but would not solve:

- profile mismatch;
- gross-vs-net metric mismatch;
- absence of replacement comparison;
- absence of willingness/thesis semantics;
- same-threshold treatment of Put and Covered Call;
- lack of lifecycle outcome evidence.

The smallest complete fix is a profile-aware action contract plus offline calibration, not a new optimizer and not a larger threshold table.

## 4. Scope boundary

### 4.1 In scope

- Short Put and short Call Close Advice for `return_first` and `insurance_underwriting` profiles.
- Yield Enhancement short-put leg mapping and group downgrade rules when group evidence is incomplete.
- Preservation of the existing Yield Enhancement long-call convexity evaluator.
- A recommendation state separate from tier.
- Stable decision-basis fields and point-in-time policy versioning.
- Reuse and completion of fee, risk, willingness, and replacement evidence.
- Close-decision Shadow Replay capture, marking, settlement, analysis, readiness, and reports.
- Production promotion gates, rollback design, public read fields, and precise close-action labels.
- Focused changes to Daily Brief close-action semantics only; no layout/template redesign.

### 4.2 Explicit non-goals

- No automatic close, roll, replacement open, broker order, or confirmation workflow.
- No candidate filtering or ranking change in `candidate_engine.py`.
- No new Strategy Lab, optimizer, score-search service, or tick-coupled tuning process.
- No reconstruction of historical option marks that were never captured.
- No inference of assignment, exercise, called-away, or settlement outside canonical ledger/reconciliation facts.
- No redesign of the current Daily Brief sections, Feishu post paragraphs, funds block, or candidate copy.
- No long-call take-profit/salvage threshold change in this work unit.
- No live config change, notification behavior change, release, or remote deployment before the promotion gate and explicit approval.
- No permanent runtime switch that keeps legacy and new formal policies active in parallel.

## 5. Target decision contract

### 5.1 Separate four concepts

Each row must keep these concepts independent:

| Concept | Field | Meaning |
|---|---|---|
| Economic/thesis nature | existing `exit_state` / `exit_reason_type` | Why closing may be economically or strategically relevant |
| Evidence urgency | existing `tier` | Relative priority; not an action by itself |
| Recommendation | new `recommendation_state` | `hold`, `review`, `close`, `not_evaluable` |
| User-facing action | existing `close_action` plus one new value | Strategy/leg-specific projection of the recommendation |

New stable recommendation contract:

```text
hold            no user action requested
review          human review requested; no directional execution claim
close           advisory close recommendation; still not an order
not_evaluable   evidence is insufficient for a safe recommendation
```

`close_action` adds only one generic review action:

```text
review_close
```

Yield Enhancement may render the same recommendation as “建议复核 Put/Call 组合” without adding a second review state.

### 5.2 New additive fields

| Field | Type | Meaning |
|---|---|---|
| `policy_version` | string | Stable policy token, initially shadow-only |
| `recommendation_state` | enum | `hold`, `review`, `close`, `not_evaluable` |
| `decision_basis` | `tuple[str, ...]` in domain; semicolon-delimited tokens in CSV | Stable reasons that caused the recommendation |
| `decision_evidence_status` | enum | Reuse/project `complete`, `partial`, `review_required`, `not_evaluable` |
| `legacy_policy_result` | shadow only object | Frozen current-policy result for paired comparison |
| `proposed_policy_result` | shadow only object | Candidate-policy result; never read by production selector before promotion |

Do not add confidence percentages. Evidence is not calibrated enough to justify probabilistic confidence.

### 5.3 Authority rules

- Domain policy owns `recommendation_state` and `decision_basis`.
- Runner owns data assembly and strategy/leg projection only.
- Notification selector uses `recommendation_state`, not `tier` alone.
- Renderer maps already-decided actions to text and never upgrades `review` to `close`.
- Shadow replay may evaluate multiple policy variants, but runtime formal Close Advice evaluates one authoritative policy only.

Compatibility rule:

- New rows always emit all four additive decision fields.
- Old artifacts with no `recommendation_state` are read as `policy_version=legacy_p0` and derive a read-only recommendation from existing `close_action` first, then `exit_state/tier`.
- The fallback exists only in readers/materializers. It cannot be used by the production selector or produce a new notification.
- `decision_basis` tokens are ordered, unique, lower snake case, and stable public values; renderers do not parse free-form `reason`.

## 6. Normalized decision facts

The domain evaluator receives one immutable facts payload assembled at a single decision timestamp.

### 6.1 Identity and provenance

- account, market, `position_lot_id`;
- symbol, option type, side, expiration, strike, contracts, multiplier;
- strategy family, strategy profile, strategy source, strategy group ID, leg role;
- quote timestamp, decision timestamp, run ID, policy version.

### 6.2 Execution and economics

- bid, ask, close mid, spread ratio, quote/evaluation status;
- premium, capture ratio, remaining premium;
- estimated close fee/status/basis;
- estimated close P&L gross/net;
- buy-to-close cost;
- legacy remaining annualized return;
- capital-basis type and amount;
- normalized remaining premium annualized return on that capital basis.

Capital basis is explicit:

- cash-secured Put: strike liability/canonical cash-secured capacity in lot currency;
- Covered Call: market value of covered shares at the decision timestamp;
- missing reliable basis: normalized metric is unavailable, never guessed.

The existing legacy metric remains for compatibility and historical replay.

### 6.3 Thesis observations, path risk, and willingness

- IV, RV, IV/RV ratio, IV minus RV;
- absolute delta and strategy-specific delta threshold observation;
- event status/types/dates;
- remaining stress scenario/loss and reward-to-stress-loss;
- assignment/called-away acceptable value and source;
- completeness/missing fields.

No single observation becomes an inferred order. P2 uses only the states the current domain can prove:

| Normalized state | Current source | Meaning |
|---|---|---|
| `valid` | `short_vol_thesis_status=valid` | No configured IV/RV, delta, or event observation is active |
| `observe` | `short_vol_thesis_status=observe` | One or more observations are active; this requests review at most |
| `not_evaluable` | missing required short-vol inputs | Thesis evidence is incomplete; it cannot produce `close` |

`weakened` and `lost` are removed from this work unit. A deterministic loss/risk exit needs a separate risk-budget contract and must not be inferred from IV/RV, delta, event, or stress observations.

### 6.4 Replacement evidence

Reuse capital-reallocation shadow facts:

- replacement identity/rank/profile;
- same-account/market/currency/family eligibility;
- released capacity and replacement capacity requirement;
- replacement annualized return after fee and slippage;
- daily-yield advantage;
- close fee, replacement open fee, spread slippage, total switch cost;
- recovery days and comparison horizon;
- replacement status and reason.

Candidate rank remains canonical and is never reranked by Close Advice.

Replacement is deliberately separated by variant:

- P2 does not consume replacement evidence. This prevents a post-processing shadow from becoming hidden formal authority.
- P3 consumes `reallocation_status` offline after `close_advice.csv` and `portfolio_capacity_shadow.csv` exist.
- Production promotion of P3 is outside this work unit because the current call order is `close advice -> reallocation shadow`. It requires a separately approved two-phase decision pipeline.
- No S1-S7 production path reads a post-processing shadow artifact back into the domain evaluator.

## 7. Profile-aware policy truth table

### 7.1 Common gates

Applied before profile policy:

1. Non-active lifecycle -> `not_evaluable` using current lifecycle contract.
2. Missing core price, invalid price, or excessive spread -> `not_evaluable`.
3. Otherwise-actionable close with unusable fee evidence -> `not_evaluable`.
4. Otherwise-actionable profit capture with non-positive estimated net close P&L -> `hold`.
5. Evidence facts are timestamp-consistent; stale/mixed-timestamp facts cannot produce `close`.

### 7.2 `return_first`

The profile objective remains locking captured income; it does not require an underwriting thesis.

| Condition | Recommendation | Basis |
|---|---|---|
| Existing strong profit-capture rule, complete execution evidence | `close` | `profit_capture_strong` |
| Existing medium profit-capture rule, complete execution evidence | `review` | `profit_capture_medium` |
| Weak/optional/none | `hold` | existing hold reason |
| Required evidence incomplete | `not_evaluable` | explicit missing basis |

The current medium-close behavior remains a replay baseline, not the proposed default.

### 7.3 `insurance_underwriting` Sell Put

Underwriting defaults to continuing the accepted liability while thesis and willingness remain valid. Profit capture is an economic signal, not the entire decision.

| Profit-capture signal | Thesis/willingness/replacement state | Recommendation |
|---|---|---|
| strong | all required evidence complete | `close` |
| strong | thesis evidence incomplete | `review` |
| medium | thesis `valid`, assignment acceptable | `hold` |
| medium | thesis `observe`, thesis evidence incomplete, or assignment no longer acceptable | `review` |
| none/weak | thesis `valid` and assignment acceptable | `hold` |
| none/weak | thesis `observe` or assignment no longer acceptable | `review` |
| none/weak | thesis evidence incomplete and willingness not revoked | `hold` |
| any | required price/fee evidence unusable | `not_evaluable` |

Important constraints:

- A lost IV/RV edge alone is not a loss stop. It may support human review only.
- High delta alone does not force closing when assignment remains acceptable.
- `assignment_acceptable=false` requests review and never becomes a P2 close recommendation.
- A formal loss/risk exit requires its own future risk-budget contract and is not inferred here.
- P3 may add `review_switch` to its offline comparison basis; P2 never consumes that post-run status.

### 7.4 `insurance_underwriting` Covered Call

The same structure applies with Covered Call semantics:

- called-away acceptable is the strategy default;
- high delta/upside proximity alone does not force buying back the Call when called-away remains acceptable;
- called-away revocation requests review;
- replacement must preserve account, market, profile, currency, and symbol because closing releases covered shares for that symbol;
- stock directional thesis or tax-lot optimization is unavailable and therefore cannot be invented.

The truth table is identical to Sell Put after replacing assignment willingness with called-away willingness.

### 7.5 Yield Enhancement

- Short Put leg first receives its resolved profile recommendation.
- Long Call leg retains the existing convexity evaluator and actions.
- Complete paired group evidence may project a Put close into the existing group action.
- Missing group identity, quantity mismatch, missing paired quote/cost, or conflicting leg actions downgrades an otherwise-close group recommendation to `review`; it never silently executes a Put-only group interpretation.
- Capital-reallocation comparison for a single combo leg remains unsupported until a group-level replacement contract exists.

### 7.6 Near-expiry optional and weak tiers

- `optional` and `weak` remain non-notifying hold/observe facts in the proposed policy.
- They are included in replay analysis but cannot be promoted merely to increase notification volume.

## 8. Shadow policy variants

Only the offline analyzer may evaluate multiple variants. Production never selects among them dynamically.

| Variant | Purpose |
|---|---|
| `P0_current` | Exact current baseline: strong/medium -> close |
| `P1_semantic_split` | Strong -> close; medium -> review for all short-option profiles |
| `P2_profile_aware` | Truth table in Section 7; recommended promotion candidate |
| `P3_opportunity_required` | Offline-only two-stage variant: P2 base result plus post-run reallocation evidence; stress-tests whether a superior feasible replacement improves close outcomes |

Threshold grids are bounded and offline-only:

- strong remaining annualized maximum: current `4.5%` plus adjacent `3%` and `6%` sensitivity points;
- medium remaining annualized maximum: current `7%` plus `5%` and `9%` sensitivity points;
- capture thresholds: current values plus/minus five percentage points;
- replacement advantage: positive after fees/slippage with recovery inside `min(current_dte, replacement_dte)`; no extra scoring weight;
- no grid search across arbitrary combinations. Evaluate only the named policy variants and bounded sensitivity points.

The analyzer reports sensitivity; it does not auto-select “best parameters.”

## 9. Point-in-time Shadow Replay design

### 9.1 Close-decision episode

Add a Close Advice facet to existing Shadow Replay rather than a new Strategy Lab.

Every material observation receives:

```text
material_fact_fingerprint
= sha256(canonical normalized facts excluding run_id, observed_at, and rendered reason)

episode_id
= sha256(account | position_lot_id | policy_version | observed_at_utc | material_fact_fingerprint)
```

`episode_date` is stored separately for daily grouping. Repeated runs on the same trading date with the same lot, policy, and material fingerprint reuse the earliest episode; the source run IDs are appended as provenance. A new episode is created when:

- trading date changes;
- recommendation state changes;
- tier changes;
- material quote/economic bucket changes;
- willingness, thesis, or replacement status changes.

Required identity tests cover same-day `review -> close`, cross-day identical facts, duplicate reruns, and two accounts holding the same contract.

### 9.1.1 Optional close-decision facet contract

Close evidence is an optional facet of the existing dataset, never candidate rows disguised as close rows:

| File | Schema | Key |
|---|---|---|
| `close_decision_episodes.jsonl` | `shadow_replay_close_episode.v1` | `episode_id` |
| `close_decision_marks.jsonl` | `shadow_replay_close_mark.v1` | `episode_id + horizon + marked_at_utc` |
| `close_decision_outcomes.jsonl` | `shadow_replay_close_outcome.v1` | `episode_id + outcome_kind` |

- Dataset manifest remains `shadow_replay_dataset.v1`; `files` gains these optional entries only when the facet exists.
- Existing v1 readers ignore unknown optional files and retain identical candidate analysis.
- Close capture reads `close_advice.csv`, the matching position context, reallocation shadow if present, and the run manifest timestamps from one run directory.
- Marking, settlement, readiness, and analysis dispatch explicitly on the close facet; they do not feed close rows into `candidate_snapshots.jsonl`, `mark_path_snapshots.jsonl`, or `outcome_facts.jsonl`.
- All close joins use `episode_id`; instrument identity is lookup context, never the outcome primary key.

### 9.2 Point-in-time input rule

- The canonical UTC timestamp prefix in the run ID is the run-start anchor; an unparseable run ID is a capture error, never filesystem mtime fallback.
- `observed_at_utc` is the unique successful account-scoped `close_advice` audit event timestamp from that run. This is the first persisted timestamp known to be no earlier than the generated decision. Missing or ambiguous audit events are capture errors. The run-start prefix cannot be used as the decision timestamp because position context is generated after the run starts.
- A quote-native timestamp is preserved when present. If the quote row has no timestamp, `quote_time_basis=run_anchor` is allowed only when the required-data artifact is inside the same run directory; external or later artifacts fail the point-in-time check.
- Position-context `as_of_utc` must be no later than `observed_at_utc`; otherwise the episode is rejected as mixed-time evidence.
- Candidate replacement must come from the same or earlier run timestamp.
- No future mark, settlement, future candidate rank, or later config may influence the evaluated decision.
- Strategy profile comes from the lot snapshot/source precedence used by production.
- Missing historical facts stay missing; replay must not synthesize them.

### 9.3 Marks and terminal facts

Collect or reuse local evidence at bounded horizons:

- first usable mark inside deterministic windows: 1d=[1,2], 3d=[3,4], 7d=[7,9], and 14d=[14,17] calendar days after `observed_at_utc`;
- expiry/settlement fact when available;
- canonical ledger lifecycle outcome including assignment/called-away when recorded;
- underlying mark needed for stress/path interpretation;
- replacement candidate marks only when that replacement existed at decision time.

OpenD collection remains explicit `--write`; dry-run stays default. Historical points that were never collected cannot be recreated from a current chain.

### 9.4 Counterfactual outcomes

All counterfactuals use decision-time incremental value. Sunk opening premium is excluded from variant comparison because it is identical for every action.

For a short option with decision ask `A0`, future ask `Ah`, multiplier `M`, contracts `N`, decision close fee `F0`, and future close fee `Fh`:

```text
close_now_cost = A0 * M * N + F0
hold_to_horizon_incremental = close_now_cost - (Ah * M * N + Fh)
close_now_incremental = 0
hold_vs_close_regret = hold_to_horizon_incremental
```

This zero baseline means a positive `hold_to_horizon_incremental` favors holding and a negative value favors closing at the decision time. For expiry/settlement:

- expired worthless: future option close cost and fee are zero;
- cash/physical assignment or called-away: use the canonical ledger lifecycle P&L increment after the decision time when available; never add a separately modeled intrinsic payoff to the same outcome;
- closed later: use the canonical close event price/fee when available, otherwise the first usable horizon mark;
- lifecycle event with no decision-time-sliced P&L: report action/outcome alignment but leave terminal P&L inconclusive.

For P3, the close cost of the current lot is present in both `close-only` and `close-then-switch` and therefore cancels:

```text
replacement_incremental
= replacement_entry_credit
  - replacement_future_close_or_terminal_cost
  - replacement_open_fee
  - replacement_exit_fee
  - observed_entry_slippage

switch_vs_close_incremental = replacement_incremental
switch_vs_hold_incremental = replacement_incremental - hold_to_horizon_incremental
```

P3 is inconclusive unless the replacement existed at the decision timestamp, the same comparison horizon is available, and all replacement entry/exit/mark costs are usable.

Every policy variant shares the same episode marks and outcome facts. Variants change only the recommendation projection. Unsupported outcomes remain explicit `inconclusive`; no zero fill is allowed.

### 9.5 Evaluation metrics

Primary metrics:

- net P&L by episode and unique lot;
- premature-close regret: feasible hold outcome minus close-now outcome;
- avoided-loss benefit: close-now outcome minus feasible hold outcome when positive;
- switch regret/benefit after all switch costs;
- maximum adverse excursion and 95th-percentile adverse path;
- assignment/called-away outcome aligned with explicit willingness;
- transaction-cost and turnover totals.

Operational metrics:

- close/review/hold counts by profile, family, market, and account;
- unique lots notified;
- repeated reminder count per lot;
- action-state transitions;
- evidence completeness and inconclusive rate;
- false urgency: `close` recommendations later dominated by hold after cost;
- missed review: large adverse outcome from a `hold` with known thesis/willingness deterioration.

Notification volume is a guardrail, not the optimization objective.

## 10. Evidence and promotion gates

### 10.1 Dataset readiness

No production promotion unless:

- at least 30 settled unique close-decision episodes overall;
- at least 10 settled unique episodes for every profile/family segment being promoted;
- at least 80% of promoted-segment episodes have usable terminal or policy-horizon outcomes;
- mark, quote, strategy, fee, and replacement timestamps pass point-in-time checks;
- repeated ticks are deduplicated at episode grain;
- inconclusive rows and missing-data reasons are reported, not dropped.

Segments below the minimum remain on current production policy and shadow-only analysis. This is a temporary evidence boundary, not a permanent mixed-policy architecture; the final promotion decision must identify one complete promotable scope.

### 10.2 Policy quality gate

`P2_profile_aware` reaches CEO review after the mechanical readiness gate. The report then shows, against `P0_current` on exactly paired episodes:

- count, mean, median, P5/P95, and paired delta for premature-close regret;
- count, mean, median, P5/P95, and paired delta for avoided-loss benefit;
- total and per-episode close/switch transaction cost;
- `close` precision, defined as actionable close episodes whose best usable hold horizon does not beat close-now after costs, divided by actionable close episodes with a usable paired outcome;
- repeated actionable reminders per unique lot;
- every profile/family segment beside aggregate results;
- coverage denominator and inconclusive count for every metric.

No undefined `not worse` or `materially reduced` machine gate remains. No automatic statistical winner is emitted. The report provides paired facts and trade-offs; the CEO promotion artifact explicitly records which deltas are accepted or rejected.

### 10.3 Required decision artifact

Before production implementation, create a durable promotion decision recording:

- selected policy variant and exact threshold values;
- accepted evidence window and dataset identities;
- segment-level metrics and inconclusive areas;
- accepted trade-offs;
- fields/actions/config changes approved for production;
- rollback criteria;
- explicit CEO approval.

Absence of this artifact is a hard stop.

## 11. Configuration and migration design

### 11.1 Shadow phase

- No runtime config changes.
- Offline variant parameters live under Shadow Replay parameter sets.
- Formal Close Advice and notifications remain current production behavior.

### 11.2 Promotion phase

After the decision artifact, migrate the single canonical `close_advice` config to profile-scoped policy values:

```yaml
close_advice:
  enabled: true
  policy_version: profile_aware_v1
  policies:
    return_first:
      strong_remaining_annualized_max: <approved>
      medium_remaining_annualized_max: <approved>
    insurance_underwriting:
      strong_remaining_annualized_max: <approved>
      medium_remaining_annualized_max: <approved>
```

Only parameters supported by the approved evidence are introduced. P2 thesis rules reuse existing strategy facts rather than duplicating opening config. Reallocation remains an offline P3 report and is not a production P2 input.

Migration rules:

- canonical defaults and generated US/HK snapshots change together;
- validator rejects mixed legacy-flat and profile-scoped policy keys;
- normal runtime has one policy path;
- no long-lived `legacy=true` or hidden fallback;
- rollback uses the previous release/config snapshot, not a second runtime evaluator;
- lot strategy profile remains frozen by current snapshot precedence, while `policy_version` is the current deployed risk-management policy and is emitted on every evaluation.

## 12. Public and notification semantics

### 12.1 Read surfaces

Expose the additive decision fields through:

- `close_advice.csv`;
- `close_advice_read`;
- analysis/materialization views;
- Daily Decision Brief position projection;
- filter trace and replay snapshot.

Legacy fields remain during one documented compatibility window, but action consumers must move to `recommendation_state`.

### 12.2 Daily Brief

Preserve the current template and Feishu rendering. Only action semantics change:

| Recommendation | User-facing status |
|---|---|
| `close` | existing strategy-specific “建议平仓/建议平掉 Put…” |
| `review` | “建议复核持仓” or combo-specific review wording |
| `hold` | hidden from actionable holdings section, as today |
| `not_evaluable` | existing data-gap handling |

Metric label refinement is narrowly scoped:

```text
剩余年化
-> 剩余权利金毛年化
```

No other notification layout or copy work is part of this plan.

### 12.3 Selector and ordering

- Select `recommendation_state in {close, review}` rather than `tier in {strong, medium}` alone.
- Order `close` before `review`, then preserve existing stable lot/tier ordering.
- Preserve `max_items_per_account`.
- Notification delivery/diff state should suppress unchanged repeated recommendations; it must not suppress a `review -> close` transition.

## 13. Architecture and ownership

```text
PositionLot + Point-in-time Quote + StrategySnapshot
-> StrategyResolver                       src/application/strategy_policy.py
-> Evidence Assembly                      src/application/close_advice_runner.py
-> Pure Profile Policy                    domain/domain/close_advice.py
-> Leg/Combo Projection                   src/application/close_advice_runner.py
-> Formal Read/Notification Projection    application read + Daily Brief
-> Shadow Episode Capture                 src/application/shadow_replay/
-> Marks/Settlement/Analysis              src/application/shadow_replay/
-> CEO Promotion Decision                 durable gate artifact
```

Ownership invariants:

- `domain/domain/` never imports `src/`.
- Candidate ranking remains in `candidate_engine.py`.
- Reallocation consumes canonical candidate order and never introduces reranking.
- Runner does not invent profile policy.
- Renderer does not infer action from tier.
- Replay cannot write production config or formal notification state.
- Ledger outcomes are read facts; Close Advice never writes them.

## 14. Implementation slices

Implementation is paused until this plan passes `planreview`. Evidence collection and production promotion each have additional explicit stop gates.

### S1 — Decision contract and current-policy parity

**Objective**: introduce a pure recommendation contract without changing current production outputs.

**Allowed files/modules**:

- `domain/domain/close_advice.py`
- `src/application/close_advice_runner.py`
- `src/application/strategy_policy.py` only if a policy-profile projection is required
- `src/application/agent_tools/close_advice_read_impl.py`
- relevant analysis/materialization allowlists
- `docs/CLOSE_ADVICE_CONTRACT.md`
- directly relevant tests

**Exact changes**:

1. Add `recommendation_state`, `policy_version`, `decision_basis`, and evidence-status projection.
2. Implement pure current-policy parity mapping so `P0_current` remains byte-for-byte equivalent in selected rows/actions.
3. Make action mapper consume recommendation state while preserving current result.
4. Add schema/public-field tests and old-artifact compatibility tests.

**Non-goals**: no new policy behavior, config, renderer change, or replay writes.

**Completion signal**: all current Close Advice action/notification fixtures remain unchanged while new fields explain the same decision.

**Stop condition**: any hidden consumer that treats `tier` or `exit_state` as an executable action must be inventoried before S2.

### S2 — Profile-aware policies in shadow only

**Objective**: implement pure `P1`/`P2` policy functions and the offline P3 composition without changing formal Close Advice.

**Allowed files/modules**:

- `domain/domain/close_advice.py`
- `src/application/shadow_replay/parameter_sets.py`
- one narrow new close-decision adapter under `src/application/shadow_replay/`
- directly relevant tests

**Exact changes**:

1. Add immutable `CloseDecisionFacts` and `ClosePolicyResult` domain dataclasses. The facts contain only: tier, exit state, side/type, strategy family/profile, evaluation status, fee status, net close P&L, thesis status, continued willingness, close-calibration status, and combo evidence status. Reallocation is a separate application-layer P3 input.
2. Implement `evaluate_close_policy(facts, variant)` for P0/P1/P2. P0 derives exactly from current exit/tier semantics; P1/P2 follow Section 7.
3. Implement P3 only in the Shadow Replay adapter by composing the P2 result with post-run reallocation evidence; P3 is not a domain/runtime selectable policy.
4. Add table-driven tests for Put, Covered Call, Yield Enhancement Put, missing evidence, willingness changes, and replacement states.
5. Prove P1/P2/P3 are unreachable from the production selector before S6.

**Completion signal**: every truth-table row has an exact deterministic test and P0 parity remains intact.

**Stop condition**: a policy needs an unavailable fact or an inferred broker/ledger outcome.

### S3a — Close-decision facet schema and capture

**Objective**: add the optional facet schemas and capture point-in-time close episodes without changing candidate replay.

**Allowed files/modules**:

- `src/application/shadow_replay/capture.py`
- `src/application/shadow_replay/common.py`
- the S2 close-decision adapter
- research CLI/agent read metadata only as required
- directly relevant tests and docs

**Exact changes**:

1. Register the three optional facet files without changing existing required `DATASET_FILES`.
2. Capture formal P0 result plus normalized facts and P1/P2/P3 projections from one run directory.
3. Generate the Section 9.1 fingerprint/episode ID and deduplicate exact reruns.
4. Reject mixed-run timestamps and ambiguous lot matches.
5. Keep writes local and explicit; existing dataset build behavior stays byte-for-byte compatible when no close source is supplied.

**Completion signal**: synthetic fixtures produce stable close episodes while existing candidate manifests and analyses remain unchanged.

**Stop condition**: capture cannot resolve exactly one lot/run timestamp without inference.

### S3b — Close-decision marks and outcomes

**Objective**: collect deterministic horizon marks and derive decision-time incremental outcomes.

**Allowed files/modules**:

- `src/application/shadow_replay/collection.py`
- `src/application/shadow_replay/marking.py`
- `src/application/shadow_replay/settlement.py`
- `src/application/shadow_replay/common.py`
- research CLI/agent metadata only as required
- directly relevant tests and docs

**Exact changes**:

1. Dispatch close facet marking separately from candidate marking.
2. Apply the exact horizon windows and fail closed when no mark lies in a window.
3. Join canonical lifecycle facts by account + lot ID + time bounds.
4. Implement Section 9.4 incremental formulas and outcome precedence.
5. Keep dry-run default; OpenD/local dataset writes remain explicit.

**Completion signal**: hand-calculated Put/Call fixtures cover close, hold, expiry worthless, assignment, called-away, future close, missing fees, and missing marks without changing candidate settlement results.

**Stop condition**: a terminal result would require reconstructing unavailable historical data or double-counting ledger P&L.

### S3c — Close-decision readiness and status

**Objective**: expose mechanical coverage/readiness for the optional close facet.

**Allowed files/modules**:

- `src/application/shadow_replay/status.py`
- `src/application/shadow_replay/readiness.py`
- existing research read/CLI surfaces
- directly relevant tests and docs

**Exact changes**:

1. Report episode, mark-window, terminal/lifecycle, fee, and paired-policy coverage separately.
2. Apply the 30 overall / 10 per segment / 80% usable-outcome readiness gates.
3. Preserve all inconclusive reason counts.
4. Leave candidate readiness output unchanged when the close facet is absent.

**Completion signal**: deterministic fixtures prove pass/fail boundaries and backward compatibility.

**Stop condition**: readiness requires a judgmental policy-quality threshold rather than mechanical evidence coverage.

### S4 — Paired policy analysis and review report

**Objective**: compare P0/P1/P2/P3 and bounded thresholds on identical episodes.

**Allowed files/modules**:

- `src/application/shadow_replay/analysis.py`
- `readiness.py`
- report renderer/read surface under existing research namespace
- directly relevant tests and docs

**Exact changes**:

1. Compute Section 9 metrics at episode, unique-lot, profile/family, market, and aggregate levels.
2. Report coverage and inconclusive reasons beside every comparison.
3. Emit no automatic parameter recommendation; quality deltas are descriptive and require CEO judgment even when readiness passes.
4. Keep rejected/held evidence in the comparison set.

**Completion signal**: a deterministic paired report shows current-vs-proposed actions and outcomes without hindsight leakage.

**Stop condition**: paired facts cannot be computed without hindsight or a segment is below mechanical readiness.

### S5 — Evidence collection and CEO policy decision

**Objective**: produce the durable promotion decision.

**Prerequisites**: S1-S4 accepted; explicit approval for any `collect-marks --source opend --write` or other local evidence write that calls external data.

**Actions**:

1. Inventory existing datasets read-only.
2. Run dry-run data plans.
3. With approval, collect/settle until readiness or document why readiness cannot be reached.
4. Generate paired analysis.
5. Present segment-level trade-offs and request CEO selection.

**Completion signal**: approved promotion decision artifact or explicit decision to remain shadow-only.

**Hard stop**: no production-policy implementation without explicit CEO approval.

### S6 — Single-path production promotion

**Objective**: promote only the approved policy and remove tier-only action authority.

**Allowed files/modules**:

- domain policy and runner/action mapper
- canonical config defaults/validation/build outputs
- close read/analysis projections
- Daily Brief close-action mapping/selector
- Close Advice contract, README, AGENT_WIKI
- directly relevant tests

**Exact changes**:

1. Apply approved policy/threshold values.
2. Make `recommendation_state` formal authority.
3. Add `review_close` projection and precise metric label.
4. Migrate canonical config in one path and reject mixed schema.
5. Preserve long-call behavior and candidate ranking.
6. Add old-artifact read compatibility without allowing legacy rows to become executable.

**Completion signal**: a 7%-only underwriting medium row is hold/review according to approved evidence, never unconditional close; selected strong and evidence-backed conditions retain the approved close action.

**Stop condition**: notification/action parity differs outside the approved truth table.

### S7 — Offline validation, canary design, and release readiness

**Objective**: prove the promoted policy is safe before any release/deployment.

**Actions**:

1. Run focused and full offline suites.
2. Generate a no-send/no-config-write comparison report against current policy.
3. Verify action counts, state transitions, public schemas, and exact Daily Brief wording.
4. Prepare a canary checklist limited to one approved account/market and no automatic action.

Actual config mutation, notification canary, release, push, and remote deployment require their normal explicit approvals and are outside the design gate.

## 15. Validation plan

### 15.1 Domain and policy

```bash
./.venv/bin/python -m pytest \
  tests/test_close_advice_domain.py \
  tests/test_close_advice_contract.py \
  tests/test_close_advice_action_policy.py
```

Expected assertions:

- exact P0 parity;
- every profile truth-table row;
- 7%-only underwriting medium does not map to unconditional close in P2;
- missing evidence cannot map to close;
- positive net economics remain required;
- long-call behavior unchanged.

### 15.2 Runner/read/notification projection

```bash
./.venv/bin/python -m pytest \
  tests/test_close_advice_runner.py \
  tests/test_notification_compact.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_scenarios.py
```

Expected assertions:

- selector follows recommendation state;
- review renders as “建议复核持仓”;
- hold remains hidden from actionable holdings;
- current Daily Brief structure and `HK$`/mid/estimated wording remain unchanged;
- unchanged recommendations are not repeatedly promoted as transitions.

### 15.3 Replay

```bash
./.venv/bin/python -m pytest \
  tests/test_shadow_replay.py \
  tests/test_shadow_replay_candidate_impact.py \
  tests/test_close_advice_reallocation_shadow.py
```

Add focused close-decision replay tests for:

- point-in-time joins;
- future-data leakage rejection;
- repeated-tick deduplication;
- missing mark/settlement facts;
- fee/slippage inclusion;
- assignment/called-away lifecycle facts;
- paired P0/P2 episode comparison;
- segment readiness and inconclusive reporting.

### 15.4 Architecture/config

```bash
./.venv/bin/python -m pytest \
  tests/test_architecture_guards.py \
  tests/test_strategy_policy.py \
  tests/test_global_liquidity_filters.py \
  tests/test_config_yaml.py
```

Expected assertions:

- domain import boundaries hold;
- candidate rank ownership unchanged;
- strategy snapshot precedence unchanged;
- mixed legacy/new config is rejected after promotion;
- generated runtime configs match canonical config.

### 15.5 Full offline baseline

```bash
./.venv/bin/python -m pytest
```

No network, Feishu send, broker write, option-position write, ledger write, or production config write is allowed in validation.

## 16. Documentation decision

Update only when the corresponding slice changes a public contract:

- `docs/CLOSE_ADVICE_CONTRACT.md`: decision states, truth table, evidence, promotion boundary;
- `docs/AGENT_WIKI.md`: read/replay commands and safety boundaries;
- `README.md`: formal-vs-shadow behavior and user-facing action semantics;
- config examples/reference only in S6 after approval.

Do not document proposed thresholds as production facts before promotion.

## 17. Risks and mitigations

| Risk | Classification | Mitigation/owner |
|---|---|---|
| Insufficient settled close episodes | requiring explicit user decision | Remain shadow-only; do not relax readiness silently |
| Historical marks cannot be recovered | assigned to evidence lifecycle | Collect forward; report coverage gap |
| Segment results conflict | requiring explicit user decision | Promote no mixed hidden policy; narrow scope or collect more evidence |
| Recommendation-state consumer missed | fixed in S1/S6 | Inventory all tier/exit/action consumers and add architecture tests |
| More review notifications create noise | fixed in S4/S6 | Measure unique-lot repeats; transition-aware delivery |
| Replacement report uses stale candidate facts | fixed in S3a/S4 | Point-in-time timestamp checks and fail-closed joins |
| Underwriting risk exit is under-specified | assigned to later work unit | This plan allows review, not inferred loss-stop execution |
| Covered Call stock/tax thesis unavailable | assigned to later work unit | Do not infer; retain called-away willingness boundary |
| Combo group replacement economics incomplete | covered by S2 truth table | Downgrade to review, preserve leg/group evidence |
| Config migration causes dual authority | fixed in S6 | One canonical schema, reject mixed forms, release rollback only |

No residual risk may remain unclassified at slice closeout.

## 18. Why this is not over-engineered

- It reuses the existing domain owner, strategy resolver, reallocation shadow, Shadow Replay, ledger facts, and Daily Brief.
- It introduces one necessary semantic separation—recommendation state from tier—instead of a new scoring engine.
- It evaluates four named variants and bounded sensitivities rather than an optimizer or arbitrary parameter search.
- It does not add automated trading, dynamic policy selection, or hidden config fallback.
- It delays production schema/config work until evidence and CEO approval exist.
- It keeps long-call and candidate-ranking policy out of scope.

## 19. Gate and execution order

```text
plan
-> planreview
-> plan fix / re-review
-> accepted plan checkpoint
-> S1 review loop
-> S2 review loop
-> S3a review loop
-> S3b review loop
-> S3c review loop
-> S4 review loop
-> S5 evidence readiness + CEO decision STOP GATE
-> S6 production promotion review loop
-> S7 aggregate validation/deepreview
-> draft PR / PR review / final closeout
```

The user has authorized implementation. The next entry point is plan re-review; after an accepted plan checkpoint Gateflow proceeds through S1-S4 automatically, then stops at S5 if evidence collection or CEO policy selection needs explicit approval.

## 20. Completion report format

Final implementation closeout must report:

- selected policy and exact evidence-backed thresholds;
- what changed in formal action semantics;
- replay datasets, coverage, and paired outcome metrics;
- policy findings and disposition;
- notification/action count changes;
- tests and offline/full-suite results;
- docs/config migration status;
- residual risks and owners;
- canary/release/remote status separately, without implying deployment unless completed.
