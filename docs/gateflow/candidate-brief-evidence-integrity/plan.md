# Gateflow Plan — Candidate Brief Evidence Integrity

- Work unit: `candidate-brief-evidence-integrity`
- Gate: `plan`
- Date: 2026-08-12
- Status: accepted after PlanReview re-review
- Branch: `fix/candidate-brief-evidence-integrity`
- Base: `origin/main@ded8f882`
- Goal artifact: `docs/gateflow/candidate-brief-evidence-integrity/goal-confirmation.md`
- Failed review: `docs/reviews/plan-review-20260812-081937.md`
- Accepted re-review: `docs/reviews/plan-review-20260812-083249.md`
- Artifact path: `docs/gateflow/candidate-brief-evidence-integrity/plan.md`

## Goal, motivation, and completion signals

The shared HK/US candidate path must distinguish a deterministic non-viable contract from an unevaluable contract,
and the scheduled Daily Brief must report only real missing evidence. Completion requires all seven success signals
in the confirmed goal artifact: correct `net_premium_non_positive` projection, preserved fail-closed inputs, concrete
sealed reason propagation, `fetched` success handling, configuration-gated CC+LP evidence, truthful AI fallback copy,
and deterministic HK/US regressions.

## Non-goals and safety boundary

- Do not change earnings policy, strategy thresholds, ranking, capacity, candidate ordering, or strategy enablement.
- Do not change public schemas or add a parallel classifier, snapshot, state machine, storage layer, or legacy fact source.
- Do not fix DeepSeek provider failures in this work unit.
- Do not modify authored/generated runtime config, secrets, production artifacts, Feishu, broker, or ledger state.
- Do not rerun production, notify, release, deploy, upgrade, merge, approve, or mark the draft PR ready.

## Goal alignment

| Plan item | Confirmed success signals |
|---|---|
| Slice 1: deterministic calculation outcome and sealed reason projection | 1, 2, 3, 7 |
| Slice 2: Daily Brief gap and AI wording integrity | 3, 4, 5, 6, 7 |
| Focused plus cross-path validation | 1–7 |

Two slices are the minimum useful split. Slice 1 establishes the source fact semantics consumed by every market and
strategy. Slice 2 changes only downstream report interpretation and wording. Splitting by each file would add review
cost without an independently useful behavior increment; combining both slices would make it harder to prove that a
renderer change did not mask an upstream classification defect.

## Design document alignment

There is no user-supplied design document. This plan follows the existing authority boundary documented and enforced
in source: Candidate Engine owns formal calculations and strategy decisions; the account/run-bound sealed
`opening_candidate_snapshot.v1` owns report facts; Daily Brief projects those facts; AI may not recalculate or
reclassify them.

## First-principles judgment and direct code evidence

A deterministic value computed from complete inputs is not missing evidence. `net_premium_non_positive` occurs only
after ready opening status, fresh quote validation, positive bid/ask/tick/multiplier/spot/IV, ready term-matched RV,
and fee calculation. It is therefore a definitive economic rejection. Other calculation failures remain fail-closed
unless their stable reason is explicitly proven definitive.

Direct ownership evidence:

- `domain/domain/engine/candidate_engine.py::calculate_opening_candidate_metrics()` raises the stable
  `net_premium_non_positive` reason after fee-adjusted net premium is calculated.
- `src/application/candidate_scanning.py::_calculation_decision_record()` currently maps every opening-ready
  calculation error to `input_invalid`; `evidence_summary_from_decisions()` treats that category as unavailable.
- `src/application/opening_candidate_snapshot.py::candidate_universe_summary()` reads the sealed scopes but lets a
  generic strategy reason win over contract reject detail already stored in `rejects[].metric_value.reason_code`.
- `src/application/daily_decision_brief_service.py::_append_prefetch_gaps()` omits `fetched` from success states and
  `assemble_daily_decision_brief()` loads CC+LP evidence unconditionally.
- `src/application/ai_decision_advice/render.py::render_family_advice_lines()` claims raw ranking is displayed for
  every unavailable outcome, although the Daily Brief renderer already has the actual per-family candidate rows.

## Contracts and interface decisions

### Candidate evidence projection

- Add one private, explicit set of definitive calculation reasons at the application evidence-projection boundary,
  initially containing only `net_premium_non_positive`.
- For an opening-ready contract whose calculation reason is in that set, use the existing canonical
  `REJECT_POLICY_REJECTED` as the decision's top-level reject reason and preserve the stable calculation code in
  `metric_value.reason_code`. `evidence_summary_from_decisions()` will therefore count it as `policy_rejected`, not
  as unavailable, while the exact economic cause remains auditable. Do not expand `CANDIDATE_REJECT_REASONS` or add
  a schema field.
- All other opening-ready calculation failures keep the existing `input_invalid` top-level reason and nested stable
  diagnostic reason. Non-ready contracts retain their existing canonical `contract_ineligible` or
  `evidence_unavailable` reason.
- When `candidate_universe_summary()` sees an unresolved-only contract scope, derive its concrete diagnostic from the
  existing sealed reject payload (`metric_value.reason_codes[0]`, then `metric_value.reason_code`, then top-level
  reason). A concrete contract cause replaces only the generic strategy reason for that same symbol/mode. If several
  causes exist, choose the lexicographically first stable code so output remains deterministic. No snapshot bytes or
  schema are rewritten.

### Daily Brief evidence requirements

- Treat `fetched` as an explicit success state in the same canonical prefetch success vocabulary as `ok`, `ready`,
  `completed`, and `cached`. Failure handling and aggregate-error fallback remain unchanged.
- Add a private current-market predicate for whether any configured symbol has enabled Combo Yield with variant
  `cc_lp`, using the existing `resolve_yield_enhancement_cfg()` and `derive_yield_enhancement_policy()` helpers.
  Call `_load_cc_lp_snapshot_family()` only when that predicate is true. Missing CC+LP evidence remains a gap when
  required; configured `sp_lc`, disabled Combo Yield, absent symbols, and other markets do not require it.
- Preserve the concrete `reason_code` already returned by `candidate_universe_summary()` in the existing Daily Brief
  gap. The renderer maps known stable evidence causes such as `term_matched_rv_unavailable` to concise Chinese; an
  unknown code falls back to the existing generic warning without exposing an invented explanation.

### AI fallback wording

- Extend the internal renderer call with an optional `has_raw_candidates` fact computed from the actual Daily Brief
  candidate rows for that family. This is presentation input, not a new persisted field.
- For unavailable AI with candidate rows, preserve the current raw-ranking fallback. With no candidate rows, state
  only that AI advice is unavailable and there is no displayable raw strategy ranking; do not claim a ranking follows.
- Existing legal `zero_candidate` and `not_applicable` branches retain priority and behavior.

## Pre-existing user change preservation and isolated execution protocol

After the clean preflight, the user introduced and then explicitly claimed five in-progress files that must remain
local, intact, and outside this work unit's protected commits unless a new goal confirmation says otherwise:

- `docs/AI_DECISION_ADVICE_DESIGN.md`
- `docs/DEPENDENCY_GRAPH.md`
- `docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md`
- `src/application/daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_renderer.py`

After a second ownership stop, the user confirmed the four originally protected files at SHA-256
`d87ca2ff35186e42a791afb6be5311f52b4c91e1f5d24075303a02e59c5f4810` and paused external editing. The five-file
protected patch snapshot, including `docs/DEPENDENCY_GRAPH.md`, is SHA-256
`cf0239869b5132f9abc7e0531395854622071d7fe9917710df35484bf46885b9`. A later read-only check showed more saves in
the protected files despite the pause. Their bytes are therefore not a stable implementation base and Gateflow will
not edit, stage, stash, revert, or depend on them.

The user separately authorized the existing `fetched` status hunk in
`src/application/daily_decision_brief_service.py` and its regression in
`tests/test_daily_decision_brief_service.py` for inclusion in Slice 2. Their acknowledged two-file patch fingerprint is
`d3bc5a10e794f47d438c607f5e32806ba020983c18c6c056ac0338b693c14a8d`. They are unreviewed implementation input,
not accepted code merely because they already exist in the worktree.

Execution isolation replaces overlapping hunk staging:

1. In the primary dirty worktree, stage and commit only the reviewed Gateflow plan/review artifacts by exact path.
   Inspect the full cached patch and confirm all user/business-code files remain unstaged.
2. Create a local clone under a `mktemp -d` path from the accepted plan commit. Check out the same work-unit branch in
   that clone; all implementation, tests, code reviews, subsequent commits, push, and draft PR operations occur there.
3. Reimplement the authorized `fetched` behavior from the accepted plan in the clean clone and review it normally;
   do not copy the dirty worktree as an implementation authority.
4. Because the isolated clone starts from committed `origin/main@ded8f882` plus the accepted plan, its baseline
   renderer/test files contain no compact-card, event-copy, heading-removal, reminder-aggregation, capacity-copy, or
   other user hunks. Whole-file staging is allowed only inside this clean clone after full cached-patch inspection.
5. Validate every accepted implementation commit from committed `HEAD` in the isolated clone; after Slice 2 and the
   aggregate/PR review commits, also use a second temporary clean clone or worktree when needed to prove no uncommitted
   artifact/code dependency.
6. Do not pull, reset, rebase, or synchronize the primary dirty worktree to the advanced branch. Leave its user
   changes untouched and report that its local branch ref intentionally remains at the accepted-plan checkpoint.

## Implementation slices

### Slice 1 — Candidate evidence classification and concrete sealed reason

- Objective: make the shared candidate source facts correctly distinguish deterministic rejection from unavailable
  evidence and expose the specific sealed cause for genuinely unresolved scopes.
- Expected outcome: tiny fee-negative US/HK contracts complete as lawful no-candidate policy rejects; RV/binding
  failures remain unavailable; affected snapshot scopes carry `term_matched_rv_unavailable` instead of a generic
  strategy reason.
- Allowed files/modules:
  - `src/application/candidate_scanning.py`
  - `src/application/opening_candidate_snapshot.py`
  - `tests/test_scan_volume_gate_min_zero.py`
  - `tests/test_candidate_scanning_evidence.py`
  - `tests/test_opening_candidate_snapshot.py`
- Exact allowed changes:
  - add the private definitive-reason projection and use it in `_calculation_decision_record()`;
  - add a private sealed reject-detail extractor and deterministic generic-reason replacement in
    `candidate_universe_summary()`;
  - add/adjust only tests needed to prove US/HK definitive rejection, unchanged fail-closed cases, accepted-count
    invariants, and specific reason projection.
- Invariants/error handling:
  - do not infer definitiveness from message text, numeric sign, market, or option side;
  - use only canonical `REJECT_POLICY_REJECTED` at the top level; retain `net_premium_non_positive` only as nested
    detail and do not change Candidate Engine's reject vocabulary;
  - an unknown calculation reason remains `input_invalid`;
  - mixed definitive and unavailable rejects retain the current unresolved/diagnostic semantics;
  - snapshot validation and hashes are not bypassed or mutated.
- Non-goals: no fee formula, Candidate Engine policy, snapshot schema, ranking, or report rendering changes.
- Validation:
  - `./.venv/bin/python -m pytest -q tests/test_scan_volume_gate_min_zero.py tests/test_candidate_scanning_evidence.py tests/test_opening_candidate_snapshot.py`
  - expected assertions: `net_premium_non_positive` yields top-level `policy_rejected`, nested
    `reason_code=net_premium_non_positive`, `policy_rejected_count=1`, unresolved count `0`, and
    `("completed", "no_candidate")` across a US Sell Put and HK Covered Call; multiplier/RV failures remain
    unavailable; concrete RV reason wins over `partial_data`; decision payload validation succeeds without vocabulary
    changes.
- Completion signal: focused tests pass and the slice diff contains only the listed source/tests plus Gateflow artifacts.
- Stop condition: any additional reason appears to require definitive classification, or changing the sealed public
  schema becomes necessary; record and stop for scope confirmation instead of broadening the set.

### Slice 2 — Daily Brief gap and AI wording integrity

- Objective: remove false Daily Brief gaps and make remaining warnings and AI fallback wording match actual facts.
- Prerequisite: accepted Slice 1 commit.
- Expected outcome: successful `fetched` rows and configured-off CC+LP create no gaps; required CC+LP failures still do;
  true RV gaps render specifically; unavailable AI without rows does not claim raw ranking is shown.
- Allowed files/modules:
  - `src/application/daily_decision_brief_service.py`
  - `src/application/daily_decision_brief_renderer.py`
  - `src/application/ai_decision_advice/render.py`
  - `tests/test_daily_decision_brief_service.py`
  - `tests/test_daily_decision_brief_renderer.py`
  - `tests/test_ai_decision_advice_render.py`
- Exact allowed changes:
  - add `fetched` to prefetch success handling;
  - gate CC+LP snapshot loading with existing resolved current-market configuration facts;
  - render known concrete partial-data causes without changing their sealed values;
  - pass actual per-family candidate presence into AI advice rendering and select truthful unavailable copy;
  - add focused positive and counterexample tests for each branch.
  - implement renderer changes only in the isolated clean clone; do not copy or depend on the primary worktree's
    compact-card changes.
- Invariants/error handling:
  - malformed or failed prefetch statuses still create gaps;
  - an enabled `cc_lp` variant still fails closed when its snapshot is missing/invalid;
  - a specific cause is rendered only from sealed `reason_code`; unknown causes use generic wording;
  - zero-candidate, candidate-present, candidate-alert, and blocked report behavior remain unchanged outside the copy
    selected for unavailable AI.
- Non-goals: no AI orchestration/provider changes, no config mutation, no new Brief schema, and no changes to candidate
  counts/actions.
- Validation:
  - `./.venv/bin/python -m pytest -q tests/test_daily_decision_brief_service.py tests/test_daily_decision_brief_renderer.py tests/test_ai_decision_advice_render.py`
  - expected assertions: `fetched/ok` has no gap; `sp_lc` has no CC+LP gap; enabled `cc_lp` missing snapshot does;
    term-matched RV warning is specific; no-row unavailable copy omits the claim that raw ranking follows, while
    candidate-present unavailable copy retains it.
- Completion signal: focused tests pass and rendered HK/US fixtures contain only evidence-backed reminders.
- Stop condition: exact variant enablement cannot be derived from the config supplied to the assembler, or truthful
  copy would require a persisted public field; stop and re-confirm the contract rather than guessing.

## Aggregate validation and expected assertions

After both accepted slice commits:

1. `./.venv/bin/python -m pytest -q tests/test_candidate_scanning_evidence.py tests/test_scan_volume_gate_min_zero.py tests/test_opening_candidate_snapshot.py tests/test_daily_decision_brief_service.py tests/test_daily_decision_brief_renderer.py tests/test_ai_decision_advice_render.py`
2. Run related candidate scan/steps, opening snapshot, Daily Brief scenario/domain/repository, and AI Advice tests selected
   by `rg` from the changed call paths; expand to the full suite if aggregate DeepReview identifies cross-path risk.
3. `./.venv/bin/python -m compileall -q domain src tests`
4. Run the repository's configured static analyzer if present and scoped to changed files; record an unavailable tool
   rather than silently claiming it passed.
5. `git diff --check` and `git status --short` before every protected commit.
6. After Slice 2 and aggregate review commits, repeat the focused suite from committed `HEAD` in an additional clean
   checkout; record both isolated implementation-tree and clean-commit results.

Expected aggregate behavior:

- US and HK use identical evidence semantics;
- Sell Put and Covered Call consume the shared corrected projection;
- real missing RV/quote/binding remains fail closed;
- lawful no-candidate remains evaluable and does not produce false evidence warnings;
- no candidate/action/rank/capacity values are changed by the report fixes.

## Docs decision

No public user/operator documentation change is planned because no command, config key, schema, workflow, or safety
boundary changes. The Gateflow goal, plan, reviews, implementation artifacts, and final closeout are the durable audit.
If implementation changes a public payload or operator-visible contract beyond the confirmed wording correction, stop
and revise this decision through goal confirmation.

## Risks, open questions, and residual-risk destinations

- Risk: a future deterministic calculation reason could repeat this issue. Current work deliberately whitelists only
  the proven `net_premium_non_positive` case; expanding the taxonomy is assigned to a later work unit with evidence.
- Risk: DeepSeek provider failures can still make AI Advice unavailable even after wording becomes truthful. Assigned
  to the separately identified AI provider reliability work unit.
- Risk: no production replay is authorized, so final validation proves deterministic source behavior, not a new live
  market outcome. A release/upgrade and post-deploy scheduled-run verification require separate user authorization.
- Risk: explicitly symbol-filtered manual runs receive `base_cfg` at the current Daily Brief call site, so CC+LP
  enablement can be broader than their exact filtered symbol scope. The confirmed target is the scheduled fixed
  report; exact manual subset config propagation is assigned to a later work unit unless implementation proves it can
  be closed without adding cross-module scope.
- Risk: the five acknowledged user files may continue changing concurrently. This is `fixed in the current plan` by
  moving all implementation and later commits to an isolated clean clone; the primary dirty tree is no longer an
  implementation or validation input.
- Open questions: none blocking at plan time.

Every residual risk above is classified: taxonomy expansion, provider reliability, production replay, and manual
symbol-subset propagation are `assigned to later work unit`; concurrent primary-tree edits are `fixed in the current
plan` through isolated execution. None is silently accepted as completed work.

## Completion report format

Final closeout will record: accepted commits per gate, changed behavior, exact validation commands/results, PlanReview
and DeepReview finding dispositions, docs decision, classified remaining risks/owners, draft PR URL, issue-link status
(not an issue unless a number is later supplied), and the next user-authorized entry point after merging the draft PR.
