# Gateflow Plan — Earnings Near-Expiry Window

- Work unit: `earnings-near-expiry-window`
- Gate: `plan`
- Date: 2026-08-11
- Status: accepted after PlanReview re-review (`pass-with-risks`)
- Branch: `feat/earnings-near-expiry-window`
- Base: `origin/main@8902f9fd`
- Goal artifact: `docs/gateflow/earnings-near-expiry-window/goal-confirmation.md`
- Failed PlanReview: `docs/reviews/plan-review-20260811-204658.md`
- Accepted re-review: `docs/reviews/plan-review-20260811-205051.md`
- Artifact path: `docs/gateflow/earnings-near-expiry-window/plan.md`

## Goal and acceptance contract

Replace the blanket “any pending earnings event before expiration blocks the contract” rule with one shared,
versioned 6-calendar-day near-expiry policy. The policy must be calculated from market-local dates, preserve distant
events as non-blocking context, fail closed only when hard-window evidence is insufficient, and isolate unavailable
contract scopes so fully evidenced candidates can still be ranked and advised with an incomplete-universe disclosure.

The canonical predicate is:

```text
pending = earnings_date >= market_local_scan_date
days_before_expiration = (expiration_date - earnings_date).days
blocking = pending and 0 <= days_before_expiration <= 6
```

The implementation is accepted only if all of the following remain true together:

- day 0 and day 6 block; day 7 and farther do not;
- a scan-day event stays pending for the entire local calendar day;
- a known blocking event is a conclusive rejection;
- without a known blocker, incomplete hard-window coverage is evidence-unavailable and fail closed;
- incomplete soft-window coverage never grants or removes hard eligibility, but remains visible as contextual evidence;
- Sell Put, Covered Call, and Combo Yield Funding Put consume exactly the same policy;
- accepted candidates plus outcome-unresolved sibling contracts remain eligible for AI evaluation, with a deterministic
  partial-universe disclosure;
- zero accepted candidates plus any unresolved hard-window gap do not produce substantive AI advice.

## Design alignment and ownership

There is no separate design document. The implementation follows the repository ownership contract:

- `domain/domain/engine/candidate_engine.py` owns the policy constant, classification semantics, rejection decision,
  decision evidence, and policy version;
- `src/application/earnings_calendar.py` owns OpenD interval projection and the distinction between hard coverage,
  soft coverage, blocking events, and non-blocking events;
- existing Sell Put/Covered Call underwriting continues to call Candidate Engine; Combo Yield Funding Put continues
  to reuse Sell Put underwriting, so no adapter-level policy fork is added;
- `candidate_scanning.py` and strategy scan steps own aggregation of conclusive rejection versus unavailable scope;
- `opening_candidate_snapshot.py` owns frozen candidate-universe completeness and policy binding;
- Daily Brief and AI Advice consume frozen facts and disclose incomplete scope; neither renderer recomputes policy.

The change is one atomic implementation slice. Splitting evidence semantics from the engine/status/consumer changes
would create an intermediate commit where distant events are still rejected, unavailable evidence is mislabeled, or
AI advice consumes a partial universe without disclosure. One slice keeps each committed runtime state coherent.

## Domain policy and evidence contract

### Shared versioned policy

Add a single exported domain policy definition at the Candidate Engine boundary:

- `EARNINGS_NEAR_EXPIRY_WINDOW_DAYS = 6`;
- an explicit policy version identifier included in normalized decision evidence and opening strategy policy hash;
- one pure date classifier used by both the application projection and Candidate Engine validation.

The classifier accepts ISO dates/date objects only after strict normalization and returns deterministic categories:

| Event relation | Classification | Hard eligibility effect |
|---|---|---|
| `earnings_date < market_date` | historical | ignored |
| `earnings_date > expiration` | after-expiry | ignored |
| `0 <= expiration - earnings_date <= 6` and pending | blocking | reject |
| `expiration - earnings_date > 6` and pending | non-blocking | retain as soft context |

No timestamp, timezone offset within the day, `pub_type`, actual EPS, or price movement is used to decide same-day
occurrence. Market timezone is resolved before projection; all comparisons after that point are date-only.

### Per-expiration OpenD coverage

`project_earnings_for_expiry()` will calculate an explicit hard interval:

```text
hard_start = max(market_local_scan_date, expiration_date - 6 calendar days)
hard_end = expiration_date
soft_start = market_local_scan_date
soft_end = hard_start - 1 calendar day
```

Existing provider fetches may remain in inclusive chunks of at most seven days. Failed provider intervals are
intersected with the hard and soft intervals rather than with the entire scan-to-expiration range as one status.

Before any projection is authoritative, the v2 snapshot validator must prove the complete fetch contract rather than
equate “no recorded failure” with coverage:

- `coverage_start == scan_date`, and `coverage_end` is the maximum declared expiration;
- normalized intervals are sorted, non-empty, 1–7 inclusive calendar days each, start exactly at `coverage_start`,
  end exactly at `coverage_end`, and have no gap, overlap, or duplicate;
- interval status, reason/error, row count, observation time, and result-hash combinations are valid;
- every normalized event is inside one validated `ok` interval and inside overall coverage;
- normalized `expirations_by_underlier` and `evidence_by_underlier` have exactly the same underlier/expiration keys.

The loader reprojects each requested underlier/expiration from the validated top-level events and intervals and
canonical-compares it with the stored projection. It must not accept stored derived state as a second authority.
Missing/extra intervals, stale projection, contradictory blocking classification, or an event attached only to a
failed interval makes the snapshot invalid and candidate earnings evidence unavailable.

Each projection and annotated candidate will carry enough normalized data to audit the decision:

- earnings evidence schema/policy version and window size;
- market date, expiration, hard-window start/end;
- all known pending events through expiration;
- blocking events and event dates, each with `days_before_expiration`;
- non-blocking events and event dates, each with `days_before_expiration`;
- hard coverage status/reason and overlapping failed intervals;
- soft coverage status/reason and overlapping failed intervals;
- source artifact/hash fields already present.

`earnings_has_event` remains the compatibility/display fact “any known pending event through expiration”. Candidate
eligibility must use the explicit blocking-event field and hard coverage status; it must not infer blocking from
`earnings_has_event`.

Decision precedence for one contract is:

1. if at least one valid known blocking event exists, reject with `risk_earnings_event`; this is conclusive even if
   another hard sub-interval failed, while the coverage gap remains in audit metadata;
2. otherwise, if any failed provider interval overlaps the hard interval, reject with
   `risk_earnings_unavailable`;
3. otherwise, earnings hard evidence is ready and the candidate proceeds through all other existing gates;
4. failed intervals confined to the soft interval set soft coverage partial/unavailable but do not alter hard
   eligibility.

This precedence cannot turn malformed/corrupt rows into positive evidence: only strictly normalized in-scope event
dates located in a validated successful source interval count as blockers; invalid source/artifact/schema/partition
or source-to-projection mismatch remains unavailable.

### Schema and compatibility boundary

- Bump the OpenD earnings calendar/projection schema to a new version because field meanings and same-day semantics
  materially change.
- Canonical candidate scanning requires the new schema. A v1 earnings artifact returns typed unavailable evidence;
  there is no legacy re-evaluation branch in Candidate Engine. Historical opening snapshots may still be rendered as
  already-frozen records, but are never recomputed under the new rule.
- Do not rewrite historical artifacts.
- Keep `opening_candidate_snapshot.v1` unchanged: existing content-hashed `scope_results` remains the only frozen
  candidate-completeness authority. Strengthen its validator instead of adding a second persisted summary.
- Change `strategy_policy_hash()` input schema/version and include the earnings policy version/window, ensuring an
  artifact sealed under the blanket rule cannot be adopted as if it used the six-day rule.

## Candidate Engine and strategy aggregation

Candidate Engine will validate and record the explicit earnings policy inputs:

- evidence status for hard eligibility;
- `earnings_blocking_has_event` and normalized blocking/non-blocking event lists;
- policy version/window and hard-window boundaries;
- soft coverage state for diagnostics.

Existing rejection codes remain stable:

- `risk_earnings_event` means a known event satisfies the six-day blocking predicate;
- `risk_earnings_unavailable` means no conclusive blocker was found and hard-window eligibility cannot be proven.

No RV, return, liquidity, Delta, multiplier, fee, capital, cover-capacity, or ranking formula changes.

`evidence_summary_from_decisions()` must represent two orthogonal facts for each contract:

1. **diagnostic evidence gaps**: every unavailable reason, including `risk_earnings_unavailable`, retained even if
   another gate conclusively rejects the contract;
2. **eligibility outcome**: `accepted`, `definitive_reject`, or `unresolved`.

The existing unavailable reason set is extended with `risk_earnings_unavailable`. A rejected contract is
`unresolved` only when it has at least one unavailable reason and no existing definitive policy/ineligibility reason.
If any definitive reason is also present, the outcome is `definitive_reject`; filling the earnings gap could not make
that contract eligible. `risk_earnings_event` is always definitive. The diagnostic gap remains auditable in either
case.

Count unique decision contracts exactly once. `evaluated_contract_count` is the number of decision records, not
`len(decisions) + accepted_count`; `accepted_count` must equal the accepted decision count/final rows or fail closed.

Sell Put and Covered Call scan status aggregation is based on **unresolved** contract scopes, not raw gap presence:

| Accepted candidates | Unresolved contract scopes | Strategy scan result |
|---:|---:|---|
| `> 0` | `0` | `completed`, no partial reason |
| `> 0` | `> 0` | `completed`, reason `partial_data` |
| `0` | `0` | lawful `no_candidate` |
| `0` | some but not all evaluated scopes | `partial_data` |
| `0` | all evaluable scopes unavailable | `data_unavailable` |

Known earnings-event rejections and any contract with another definitive reject are definitive outcomes, even when
diagnostic earnings gaps coexist. Combo Funding Put must inherit both the eligibility behavior and classification
from its existing underwriting call; add parity tests at that boundary rather than duplicate branches in
`combo_yield_steps.py`.

## Frozen snapshot, Advice, and Daily Brief

### Frozen candidate-universe completeness

`scope_results` remains the single content-hashed authority. Strengthen snapshot validation so strategy scope rows
have valid status/reason enums, unique `(symbol, strategy_mode)` identities, and exact correspondence with
`strategy_results`; contract rows must continue to correspond exactly to candidate decisions.

Add one pure `candidate_universe_summary(snapshot)` projection over a validated snapshot. It returns
`status = complete|partial`, affected symbol/mode scopes, and typed reasons from unresolved scope status. It is not
persisted back into the opening snapshot. The opening status remains `candidates_found` whenever at least one
accepted candidate exists and no account-wide dependency is unavailable; a partial sibling scope does not erase
those candidates. With zero candidates, existing `partial_data`/`data_unavailable` fail-closed semantics remain.

### AI Advice

The orchestration gate may evaluate a `candidates_found` snapshot whose universe summary is partial because every
candidate supplied to the model is still fully evidenced. The frozen input/context and deterministic renderer must
call the shared projection and include its result in the already-content-hashed frozen `candidates` payload; the model
must not describe the list as exhaustive. No new opening snapshot schema is introduced.

Do not relax the gate for a snapshot whose opening status is `partial_data` or `data_unavailable`, and do not add a
Combo Yield Advice adapter. A zero-candidate partial universe remains `advice_input_unavailable` rather than a
substantive recommendation.

### Daily Brief

Daily Brief will call the same scope-results projection even when strategy status is
`candidates_found`, so it can render one deduplicated warning that some contract scopes lacked hard-window evidence.
It will distinguish:

- known near-expiry blocking event;
- known distant non-blocking event;
- hard-window evidence unavailable (eligibility fail closed);
- soft-window evidence incomplete (context warning only).

`_candidate_event_risk()` will use the frozen blocking classification/window metadata. It must not independently
recreate the former “any event before expiry” attention rule.

## Implementation slice — S1

### Allowed production files

- `domain/domain/engine/candidate_engine.py`
- `domain/domain/engine/__init__.py`
- `src/application/earnings_calendar.py`
- `src/application/candidate_scanning.py`
- `src/application/sell_put_steps.py`
- `src/application/sell_call_steps.py`
- `src/application/opening_candidate_snapshot.py`
- the minimal existing AI Advice context/orchestration/render modules under
  `src/application/ai_decision_advice/` needed to carry the frozen completeness disclosure
- `src/application/daily_decision_brief_service.py`
- `src/application/daily_decision_brief_renderer.py`

`src/application/combo_yield_steps.py` is test-only unless inspection proves a missing handoff at the existing
underwriting boundary. Any production file outside this list requires a Gateflow scope amendment before editing.

### Allowed documentation and artifacts

- `docs/candidate_strategy.md`
- `docs/STRATEGY_ARCHITECTURE.md`
- `docs/AI_DECISION_ADVICE_DESIGN.md` only if its frozen-input disclosure contract changes
- `docs/gateflow/earnings-near-expiry-window/`
- `docs/reviews/` artifacts generated by PlanReview/deepreview

### Implementation order

1. Add characterization tests for days 0/6/7, same-day afternoon behavior, historical/after-expiry events, exact
   interval partitions, hard and soft failures, stored projection drift, and conclusive blocker precedence.
2. Add the shared domain policy classifier/version and update policy hashing.
3. Upgrade earnings snapshot validation/projection/annotation to emit explicit blocking, non-blocking, hard-coverage,
   and soft-coverage facts; reproject from validated source facts and preserve provenance.
4. Change Candidate Engine to validate/recompute explicit earnings facts and keep existing reject codes with corrected
   meanings.
5. Split diagnostic gaps from eligibility outcome, correct exact contract counts, and project partial-scope scan
   status for both put and call paths; verify Combo Funding Put parity through its real underwriting call.
6. Strengthen existing `scope_results` validation, add the single pure universe projection, and propagate the derived
   disclosure into AI Advice and Daily Brief without loosening zero-candidate fail-closed behavior.
7. Update focused docs and run the complete verification matrix.

### Slice prerequisites, invariants, and stop condition

- Prerequisites: confirmed goal artifact, accepted plan re-review, plan checkpoint commit, clean scoped worktree, and
  no production/config mutation.
- Call path: OpenD market fetch -> v2 snapshot validator -> per-expiration source projection -> candidate annotation ->
  Candidate Engine -> per-contract outcome aggregation -> scan status -> frozen scope results -> Advice/Brief view.
- Invariants: no consumer recomputes N independently; every accepted candidate has complete hard evidence; a source
  gap and an unresolved eligibility outcome remain distinct; all Advice-visible candidates originate from frozen
  ranked rows; zero-candidate unresolved scope never becomes substantive Advice.
- Completion signal: all focused/broad tests and static checks pass, implementation artifact records changed files,
  contract decisions, docs, residual risks, and no change outside the allowed scope.
- Stop condition: schema/source facts cannot be validated without a new user-visible assumption, an allowed file is
  insufficient and requires scope expansion, unrelated dirty work overlaps, or validation exposes an unclassified
  risk. Otherwise continue automatically to slice deepreview.

### Tests in scope

- `tests/test_earnings_calendar.py`
- `tests/test_candidate_engine_contract.py`
- `tests/test_candidate_engine_parity.py`
- `tests/test_sell_put_strategy_risk.py`
- `tests/test_sell_call_strategy_unification.py`
- `tests/test_combo_yield_steps.py`
- `tests/test_opening_candidate_snapshot.py`
- `tests/test_ai_decision_advice_orchestration.py`
- `tests/test_ai_decision_advice_contexts.py`
- `tests/test_ai_decision_advice_render.py`
- `tests/test_daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_renderer.py`
- any directly failing existing test whose fixture encodes the old blanket rule, provided the fixture change is
  behaviorally justified and documented in the implementation artifact.

## Test matrix

### Policy boundaries

- expiry minus event = 0, 1, 6 -> blocker;
- expiry minus event = 7 and 9 -> non-blocking context;
- event after expiry -> irrelevant;
- event before market date -> historical;
- event equals market date at scan times before and after the OpenD timestamp -> identical pending classification;
- market timezone date rollover -> classification uses supplied market-local date, not host/UTC date.

### Provider coverage

- complete hard and soft intervals, no event -> earnings-ready;
- blocking event known -> conclusive event reject;
- no blocker plus failed interval overlapping hard window -> earnings-unavailable reject;
- failed interval only in soft window -> candidate can proceed, contextual coverage warning retained;
- distant known event plus complete hard window -> candidate can proceed with non-blocking event evidence;
- malformed date/schema/hash -> unavailable, never clean no-event;
- missing, overlapping, duplicated, or out-of-order interval partition -> unavailable;
- stored projection inconsistent with top-level events/intervals -> unavailable;
- blocker row not backed by a successful interval -> unavailable, not conclusive event evidence;
- interval boundaries are inclusive and a failure touching hard-start is a hard failure.

### Strategy and status parity

- identical evidence yields identical earnings decision for Sell Put, Covered Call, and Combo Funding Put;
- accepted candidate plus outcome-unresolved sibling -> candidate retained, partial scope frozen and disclosed;
- earnings unavailable as the only reject -> unresolved and partial/data-unavailable as appropriate;
- earnings unavailable plus a definitive DTE/return/capacity reject -> definitive rejection with a diagnostic gap,
  not an unresolved candidate-universe scope;
- definitive rejects only -> lawful no-candidate;
- mixed definitive rejects and unavailable -> partial-data zero-candidate, Advice blocked;
- all contract scopes unavailable -> data-unavailable, Advice blocked;
- other independent gates still reject a contract after earnings passes.

### Consumer presentation

- Daily Brief does not say a day-7/day-9 event caused filtering;
- Daily Brief distinguishes known blocker from unavailable hard evidence;
- AI prompt/context and deterministic output identify a partial candidate universe when candidates remain;
- zero-candidate partial/unavailable input produces no substantive AI recommendation;
- no duplicate partial warnings across strategy/symbol/contract summaries.

## Verification commands

Run focused tests first:

```bash
./.venv/bin/python -m pytest -q \
  tests/test_earnings_calendar.py \
  tests/test_candidate_engine_contract.py \
  tests/test_candidate_engine_parity.py \
  tests/test_sell_put_strategy_risk.py \
  tests/test_sell_call_strategy_unification.py \
  tests/test_combo_yield_steps.py \
  tests/test_opening_candidate_snapshot.py \
  tests/test_ai_decision_advice_orchestration.py \
  tests/test_ai_decision_advice_contexts.py \
  tests/test_ai_decision_advice_render.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_renderer.py
```

Then run adjacent pipeline/notification regressions and static checks:

```bash
./.venv/bin/python -m pytest -q tests/test_multi_tick_*.py tests/test_notify_symbols_markdown.py
./.venv/bin/ruff check <changed-python-files>
./.venv/bin/python -m compileall -q domain src
git diff --check
```

Finally run the full suite because the candidate engine and frozen snapshot are shared cross-strategy contracts:

```bash
./.venv/bin/python -m pytest -q
```

No live OpenD request, production tick, notification send, config build/write, remote command, release, or deploy is
part of verification.

## Documentation decision

Documentation updates are required because this changes externally observable strategy eligibility and evidence
wording:

- `docs/candidate_strategy.md`: shared six-day earnings hard gate and unavailable-evidence behavior;
- `docs/STRATEGY_ARCHITECTURE.md`: Combo Funding Put inherits the same gate, no separate window;
- `docs/AI_DECISION_ADVICE_DESIGN.md`: document the scope-derived candidate-universe summary added to the frozen
  Advice candidates input.

## Risks and mitigations

1. **False eligibility from partial OpenD coverage** — hard/soft interval intersection is explicit; hard gaps fail
   closed unless a blocker already makes rejection conclusive.
2. **Policy drift across strategies** — one exported domain policy and parity tests; no per-adapter thresholds.
3. **Old artifact re-interpretation** — schema/policy hash versioning; no historical artifact rewrite.
4. **Partial universe presented as exhaustive** — frozen completeness summary is hashed and consumed by both Advice
   and Daily Brief from one validated `scope_results` projection.
5. **Same-day event may already have occurred** — intentional conservative policy confirmed by the user: same market
   calendar day remains pending.
6. **Candidate counts may still be zero** — expected when RV, return, liquidity, capacity, or another independent
   gate rejects; tests assert earnings pass is not final acceptance.
7. **Broad shared-engine blast radius** — focused cross-strategy tests plus full suite before review acceptance.

## Goal alignment

| Confirmed success signal | Plan element |
|---|---|
| N=6, natural-day inclusive, same-day pending | shared domain classifier and boundary tests |
| far event is soft, hard-window OpenD gap fails closed | v2 exact coverage plus hard/soft projection |
| only affected scope is blocked | eligibility outcome distinct from diagnostic gaps |
| SP/CC/Combo Funding Put parity | shared Candidate Engine path and Combo parity test |
| remaining candidates can be advised with warning | existing frozen scope authority plus shared universe projection |
| zero-candidate partial evidence blocks Advice | unchanged Advice gate plus explicit regression |
| auditability and policy identity | v2 evidence, decision provenance, and policy hash version |

No plan element introduces a goal outside the confirmation artifact. Exact interval validation and single-source
scope projection are necessary absence-proof and consistency conditions, not a general snapshot framework.

## Open questions

None. No implementation-time strategy or schema choice remains open after the accepted PlanReview fixes.

## Classified residual risks

- **assigned to later work unit**: OpenD may return a technically successful but factually incomplete calendar; a
  second provider or cross-provider reconciliation is outside this goal.
- **fixed in current slice**: interval gaps, projection drift, outcome/gap conflation, and inconsistent consumer
  completeness are covered by the v2 validator and focused regressions.
- **intentional confirmed policy**: same-day remains pending through local midnight even if the event already occurred.
- **assigned to later work unit**: general unavailable semantics of non-earnings RV/liquidity fields are unchanged.
- **covered by current validation**: other independent gates can still yield zero candidates and retain their reject
  provenance.

There are no unclassified residual risks.

## Completion report format

Final Gateflow closeout will report: outcome; accepted plan/slice/deepreview/PR-review commit hashes; draft PR URL and
state; changed production/tests/docs files; exact focused/static/full-suite validation results; policy/schema behavior
summary; classified residual risks; and explicit confirmation that no merge, release, deploy, remote upgrade, config
write, production tick, or real notification occurred.

## Rollback and operational boundary

The source commit can be reverted as one atomic slice. No database/config migration or historical artifact mutation
is involved. This Gateflow may push a development branch and create a draft PR after all review gates pass, but it
must not merge, release, deploy, upgrade production, restart services, run a production tick, or send notifications.

## Scope classification

- Current slice: earnings policy, evidence projection, strategy status aggregation, frozen completeness, Advice/Brief
  disclosure, focused docs/tests.
- Intentional future work: configurable windows, additional event providers, intraday actual-release detection,
  Combo Yield AI Advice, and general candidate-universe telemetry.
- Prohibited in this work unit: unrelated RV/premium/multiplier/fee/capacity/ranking changes and all operational
  mutations listed above.
- Unclassified items: none.
