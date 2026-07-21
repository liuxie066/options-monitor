# Candidate Event Risk Monitoring — Implementation Plan

- **Work unit**: `candidate-event-risk-monitoring`
- **Target release**: `1.4.0`
- **Date**: 2026-07-21
- **Revision**: 2
- **Gate**: plan
- **Baseline**: `v1.3.5` (`502c0332`)
- **Goal artifact**: `docs/gateflow/candidate-event-risk-monitoring-goal-confirmation-20260721.md`
- **Artifact path**: `docs/plans/candidate-event-risk-monitoring-plan-20260721.md`
- **Status**: revised after plan review; ready for re-review

## 1. Goal, Motivation, and Success Signal

Add candidate-bound event-risk evidence to the existing Daily Decision Brief. The current run-level event snapshot is the only Daily Brief event authority. The feature must present event facts and uncertainty safely, detect material event/evidence changes against the last confirmed-delivered brief, and reuse the v1.3.5 notification lifecycle unchanged.

Success is the nine signals in the goal-confirmation artifact, with focused regressions proving zero candidate-row fallback, zero false confirmed-none states, non-material freshness changes, safe provider degradation, concrete candidate attribution, and unchanged non-event behavior.

## 2. First-Principles Judgment and Design Alignment

A notification can only claim “no event” when the acquisition boundary proves that the relevant event categories were queried successfully. An empty event list alone is not evidence of absence. Therefore the first implementation dependency is explicit provider coverage/completeness metadata.

The Daily Brief assembler owns run-artifact composition, the domain owns candidate event semantics and materiality, and the renderer owns Chinese user projection. The repository already owns revision allocation and last-confirmed comparison and must not change.

## 3. Non-goals and Scope Boundary

The locked scope and non-goals are those in the goal-confirmation artifact. In particular:

- no new persistence or notification state machine;
- no new config keys;
- no change to existing scanning event warn/reject behavior;
- no action identity, rank, capacity, scheduler, or sender changes;
- no claim that all providers can confirm all event categories;
- no raw internal reason codes in user Markdown.

## 4. Contracts and Invariants

### 4.1 Event source payload

Existing public/direct `fetch_symbol_events_futu()` and `fetch_symbol_events_yfinance()` continue returning `list[dict]` unchanged. New internal `fetch_symbol_event_evidence_futu()` and `fetch_symbol_event_evidence_yfinance()` functions return the structured payload consumed by the orchestrator:

```json
{
  "events": [{"type": "earnings", "date": "2026-08-05", "source": "futu"}],
  "coverage": {
    "earnings": {"status": "complete", "error": ""},
    "ex_dividend": {"status": "complete", "error": ""},
    "split": {"status": "complete", "error": ""}
  }
}
```

The public list functions are thin projections of the evidence functions and preserve `om event-source probe` plus direct callers. `EventStore.resolve()` accepts structured evidence and the legacy injected `list[dict]` test/caller form. A legacy list has `coverage_status=unknown`; it can prove a concrete event but cannot prove absence. Persisted cache and run snapshot items retain coverage metadata. Partial sub-source failures no longer disappear.

Coverage statuses are internal: `complete`, `partial`, `unsupported`, `unknown`. They never appear directly in user Markdown.

### 4.2 Event identity

Each normalized event has:

- `event_series_id`: deterministic digest of canonical symbol + normalized event type; stable across date adjustments;
- `event_id`: deterministic digest of series plus the strongest provider-neutral occurrence anchor available; if no semantic anchor exists, date is used only as occurrence identity;
- `event_type`, `event_date`, and optional internal source/anchor metadata.

Material date matching first uses equal anchored `event_id`. When neither side has a semantic occurrence anchor, same-series date-change pairing is allowed only if each side has exactly one non-conflicting next occurrence and the previous occurrence date is not earlier than the current market trading date. A previous occurrence that has elapsed is never paired with the next recurring occurrence. Multiple dates, mixed anchors, or any other ambiguous match produce an unknown/conflict transition rather than an invented date adjustment or removal.

### 4.3 Candidate event-risk projection

A pure domain function receives:

- canonical symbol;
- market trading date;
- one or more relevant expirations (`contract`, or `put`/`call` for Combo Yield);
- strict run-snapshot symbol evidence.

It returns an additive `event_risk` object containing:

- `user_state`: `confirmed_event`, `confirmed_none`, or `unknown`;
- internal `reason_code`;
- `reliable` boolean;
- nearest normalized event or null;
- calendar-day distance;
- expiration relations and attention-window boolean;
- selected provider and freshness-insensitive `evidence_chain_id`;
- coverage summary sufficient for audit and removal safety.

Invariants:

- a valid concrete future event may be confirmed even when unrelated categories are incomplete;
- confirmed absence requires selected primary evidence (`source_status=ok`) and complete coverage for all three important types;
- fallback-only empty evidence, stale/error/missing/malformed/partial/conflict evidence is `unknown`;
- snapshot timestamps, cache hit labels, and fetch timing never affect event materiality;
- candidate-row event fields are ignored by the Daily Brief assembler.

### 4.4 Daily Brief additive schema

- Candidate views receive top-level `event_risk`.
- Opening candidate actions receive the same `event_risk` outside stable action-ID fields.
- Combo actions/candidates use both Put and Call expirations.
- Top-level `events` becomes a deduplicated audit list derived from candidate-bound snapshot projections, not CSV compatibility fields.
- Old persisted briefs without `event_risk` remain valid and normalize to unknown/not-observed semantics.

### 4.5 Material state transitions

Only opening actions where current or previous action is active P0/P1 participate. Existing candidate lifecycle changes continue to run first.

Material event changes:

- `candidate_event_added`;
- `candidate_event_date_changed`;
- `candidate_event_entered_expiry_window`;
- `candidate_event_evidence_degraded`;
- `candidate_event_evidence_recovered`;
- `candidate_event_removed`, only when previous/current evidence are reliable, current is confirmed-none, and `evidence_chain_id` is unchanged.

Non-material:

- snapshot/fetch/cache timestamps;
- cache-hit status with unchanged semantic evidence;
- ordering or duplicate raw source rows;
- provider diagnostics that do not change user state, event fact, date, or expiry relation.

A provider switch from primary event evidence to empty fallback evidence becomes degradation/unknown, never removal.

### 4.6 Renderer

Every displayed candidate has exactly one short event decision line:

- confirmed event: event label/date plus relation to expiration(s), followed by a recheck instruction when inside the attention window;
- confirmed none: explicitly says the current contract window has no confirmed important event;
- unknown: explicitly says event data are incomplete and absence cannot be confirmed.

Material summaries name the candidate contract via the existing human contract formatter. Raw provider/status/reason enums remain audit-only.

## 5. Affected Ownership Boundaries

### Domain

- Add `domain/domain/daily_decision_event_risk.py` for pure normalization, candidate projection, stable identity, and event-risk diff helpers.
- Modify `domain/domain/daily_decision_brief.py` only to normalize additive action/candidate event data and emit material event changes.

### Application event acquisition

- Modify `src/application/events/source_futu.py` and `source_yfinance.py` to report per-category coverage.
- Modify `src/application/events/store.py` and `orchestrator.py` to normalize/persist structured fetch payloads without changing provider selection policy.
- Modify `src/application/events/prefetch.py` summary only as required to carry the additive fields.

### Daily Brief application/rendering

- Modify `src/application/daily_decision_brief_service.py` to strictly load the current run snapshot and attach projections.
- Modify `src/application/daily_decision_brief_renderer.py` to render event lines and material summaries.
- Do not modify `daily_decision_brief_repository.py`, scheduler, sender, or confirmation pointer logic unless a failing integration test proves an existing facade cannot carry the additive fields.

### Tests/docs

- Extend existing event-prefetch/source and Daily Brief service/domain/renderer/scenario tests.
- Update README, Agent Wiki, dependency graph, CHANGELOG, and VERSION only after behavior passes focused review.

## 6. Implementation Slices

### Slice A — Authoritative event evidence contract

**Objective:** make the run snapshot able to distinguish complete, partial, unsupported, stale, error, and fallback evidence.

**Allowed files:**

- `src/application/events/source_futu.py`
- `src/application/events/source_yfinance.py`
- `src/application/events/probe.py` only if a compatibility assertion requires an additive coverage field; its existing event-list response shape must not change
- `src/application/events/store.py`
- `src/application/events/orchestrator.py`
- `src/application/events/prefetch.py`
- `tests/test_event_source_futu.py`
- `tests/test_event_prefetch.py`
- `tests/test_event_risk_warn.py` only for compatibility regressions
- slice/review artifacts

**Exact changes:** add internal structured evidence fetch functions while preserving public list-returning fetch APIs; structured payload normalization; additive cache/snapshot coverage fields; first-party per-category coverage; legacy list compatibility as unknown coverage; deterministic event dedupe/order and probe response shape preserved.

**Non-goals:** no Daily Brief changes; no provider policy/cadence changes; no config changes.

**Validation:** event tests prove partial sub-source failure survives, empty complete Futu differs from empty fallback/unknown, old list fetchers remain accepted, direct public fetch functions and `event-source probe` still expose event lists/counts, stale/error semantics remain unchanged, and candidate scanning compatibility still passes.

**Completion signal:** run snapshot has sufficient evidence to prevent false confirmed-none claims.

### Slice B — Candidate-bound Daily Brief projection

**Objective:** derive every candidate event state from the strict current run snapshot with no candidate-row fallback.

**Allowed files:**

- `domain/domain/daily_decision_event_risk.py`
- `src/application/daily_decision_brief_service.py`
- `domain/domain/daily_decision_brief.py` only for additive normalization support
- `tests/test_daily_decision_brief_service.py`
- new focused domain test if needed
- slice/review artifacts

**Exact changes:** strict snapshot load; missing/malformed handling; event normalization/identity; candidate and action attachment; Combo dual-expiry relation; audit events derived from projections; event artifact/gap diagnostics.

**Non-goals:** no material diff or Markdown rendering yet; no ranking/capacity/action-ID changes.

**Validation:** service/domain tests prove confirmed event/none/unknown, malformed/missing/partial/stale/fallback behavior, zero CSV fallback, identical action IDs/ranking, and correct Combo leg relations.

**Completion signal:** canonical brief contains safe candidate-bound event evidence without changing lifecycle behavior.

### Slice C — Material diff and user rendering

**Objective:** make approved event transitions material and render concise candidate-specific text through the existing lifecycle.

**Allowed files:**

- `domain/domain/daily_decision_event_risk.py`
- `domain/domain/daily_decision_brief.py`
- `src/application/daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_domain.py`
- `tests/test_daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_scenarios.py`
- `tests/test_daily_decision_brief_notification_flow.py` if needed to prove pointer reuse
- slice/review artifacts

**Exact changes:** six material change types, candidate action attribution, same-chain removal guard, freshness canonicalization, changed-candidate prioritization, event decision line and change summaries.

**Non-goals:** no repository/sender/scheduler rewrite; no raw enum display.

**Validation:** table-driven domain/scenario tests cover every transition and non-transition; genuine future date correction is detected; multiple-date ambiguity becomes unknown/conflict; an elapsed quarterly event is not paired with the next quarter; provider degradation never announces removal; freshness-only remains silent; material notification still sends full current snapshot and advances the existing pointer only after confirmation.

**Completion signal:** all requested event changes flow through v1.3.5 material notification semantics.

### Slice D — Integration, documentation, and release metadata

**Objective:** close public documentation and release-contract gaps after behavior is accepted.

**Allowed files:**

- `README.md`
- `docs/AGENT_WIKI.md`
- `docs/DEPENDENCY_GRAPH.md`
- `CHANGELOG.md`
- `VERSION`
- directly affected tests/artifacts if integration validation finds a real defect

**Exact changes:** document user semantics, authority/fallback boundary, material transitions, non-goals, default-off behavior, and release `1.4.0`; regenerate dependency graph if required.

**Validation:** all focused suites, agent plugin contract/smoke, config validation, dependency graph check, release check for `v1.4.0`, Ruff, `git diff --check`, then full Python 3.12 pytest when resources permit.

**Completion signal:** code/docs/release metadata agree and all required validations pass.

## 7. State Machine and Failure Handling

```text
run event prefetch
  -> write strict run snapshot
  -> assemble candidate event_risk
  -> persist Daily Brief revision
  -> compare with last confirmed-delivered revision
  -> material? render change summary + current full snapshot : remain silent
  -> provider confirms send
  -> advance existing confirmation pointer
```

Failures before Daily Brief assembly produce candidate-level unknown evidence, not a fabricated no-event state and not a second account-level lifecycle. Malformed persisted Daily Brief state continues to fail closed under the repository's existing rules.

## 8. Tests and Expected Assertions

Focused commands by slice:

```bash
./.venv/bin/python -m pytest tests/test_event_source_futu.py tests/test_event_prefetch.py tests/test_event_risk_warn.py
./.venv/bin/python -m pytest tests/test_daily_decision_brief_service.py tests/test_daily_decision_brief_domain.py
./.venv/bin/python -m pytest tests/test_daily_decision_brief_renderer.py tests/test_daily_decision_brief_scenarios.py tests/test_daily_decision_brief_notification_flow.py
```

Final validation:

```bash
./.venv/bin/python -m pytest tests/test_daily_decision_brief_*.py tests/test_event_*.py
./.venv/bin/python -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
./.venv/bin/python -m pytest
./.venv/bin/python -m ruff check <touched-python-files>
python3.12 scripts/generate_dependency_graph.py --check
python3.12 scripts/release_check.py --tag v1.4.0
./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example
./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example
git diff --check
```

Expected assertions include all user success metrics and unchanged v1.3.5 non-event scenario fixtures.

## 9. Documentation Decision

Public user-visible behavior changes, so README and Agent Wiki must describe the three states, candidate relation, material changes, authority path, and fail-closed degradation. No new CLI/config command reference is needed because public inputs are unchanged.

## 10. Why This Is Not Overdesigned

- reuses the existing event prefetch, run snapshot, action identity, Daily Brief revision store, confirmation pointer, renderer, and notification sender;
- adds one pure domain module because evidence semantics and materiality are non-trivial business rules and must not live in the assembler;
- adds no new database, receipt, service, config key, scheduler, CLI, provider, or generalized timeline;
- keeps backward compatibility additive under the existing Daily Brief schema version;
- legacy fetcher support is a narrow adapter, not a second permanent authority path.

## 11. Risks and Tracking

- **Ambiguous recurring-event occurrence matching:** fail to unknown/conflict; fixed in Slice C tests.
- **Provider completeness overclaims:** first-party adapters explicitly report per-category coverage; fixed in Slice A.
- **Old cached entries lack coverage:** treat as unknown for absence while still allowing concrete events; covered in Slice A/B.
- **Message growth:** one bounded line per already displayed candidate; renderer's existing total cap remains authority; covered in Slice C.
- **Real provider/API drift:** no live mutation or notification in this work unit; assigned to later authorized canary.

Plan-review findings PR-01 and PR-02 are accepted and addressed in revision 2. No unclassified residual risk or blocking open question remains before re-review.

## 12. Completion Report Format

Final closeout will report changed behavior, verification commands/results, docs/version updates, finding status, remaining operational risks/owners, Draft PR URL, and the next authorized release/remote-canary entry point.
