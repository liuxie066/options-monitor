# Gateflow Goal Confirmation — Candidate Event Risk Monitoring

- **Work unit**: `candidate-event-risk-monitoring`
- **Target release**: `1.4.0`
- **Date**: 2026-07-21
- **Gate**: goal confirmation
- **Baseline**: `502c0332` (`v1.3.5`, `origin/main` at branch creation)
- **Branch**: `codex/candidate-event-risk-monitoring-v1.4.0`
- **Artifact path**: `docs/gateflow/candidate-event-risk-monitoring-goal-confirmation-20260721.md`
- **Status**: confirmed by user

## Goal

Extend the v1.3.5 Daily Decision Brief so every displayed candidate carries concise, decision-ready event-risk context sourced from the current run-level event snapshot, and changes in event facts or evidence reliability participate in the existing material-notification lifecycle.

The feature must distinguish a reliable absence of events from missing, partial, stale, malformed, fallback-only absence, or conflicting evidence.

## Motivation

The current Daily Brief reconstructs `brief.events` from candidate CSV compatibility fields (`event_flag`, `event_types`, `event_dates`, and `event_source_status`). That loses evidence completeness, stable identity, provider lineage, candidate binding, expiry relation, and material change semantics. It can therefore turn unavailable event data into an apparent absence of event risk.

## Direct Code Evidence

- `src/application/account_run.py` already writes one run-level `output_runs/<run_id>/state/event_snapshot.json` before account pipelines execute.
- `src/application/events/orchestrator.py` already records selected provider, provider chain, per-provider results, source status, cache status, and event rows.
- `src/application/daily_decision_brief_service.py::_candidate_events()` currently reconstructs Daily Brief events from candidate rows instead of reading the run snapshot.
- `domain/domain/daily_decision_brief.py::diff_daily_decision_briefs()` compares action lifecycle, priority, and capacity but ignores event evidence.
- `src/application/daily_decision_brief_repository.py` already compares each new revision to the last confirmed-delivered revision and advances the pointer only after confirmed delivery.
- `src/application/daily_decision_brief_renderer.py` already renders a compact full snapshot with a material-change banner.

## Success Signals

1. Every important displayed candidate has one of three user semantics: confirmed event, confirmed no event in the candidate window, or temporarily unable to confirm.
2. Missing/partial/stale/malformed/conflicting/fallback-only absence is never rendered as confirmed no event.
3. Daily Brief event facts are derived from the run-level snapshot; candidate CSV event fields are never used as Daily Brief fallback.
4. Candidate event context includes nearest event, distance in calendar days, relation to each relevant expiration, evidence reliability, and whether it enters the strategy attention window.
5. Event additions, date changes, expiry-boundary crossings, evidence degradation/recovery, and same-chain confirmed removals are material for current or previous confirmed important candidates.
6. Freshness-only changes are non-material.
7. Provider degradation cannot announce event removal.
8. Event changes always identify a concrete candidate visible in the current full snapshot or the change summary.
9. With no event change, v1.3.5 notification behavior remains unchanged.

## Locked Scope Boundary

- Strategy attention window is the market trading date through the candidate expiration, inclusive.
- Important event types reuse the existing normalized event source types: `earnings`, `ex_dividend`, and `split`.
- Important candidates reuse the existing active P0/P1 opening-action lifecycle carriers from the current or last-confirmed Daily Brief.
- Combo Yield event relation is evaluated against both Put and Call expirations.
- `ok_with_fallback` may confirm a concrete event fact, but an empty fallback result cannot confirm absence or removal.
- Confirmed removal requires reliable complete evidence on the same selected-provider evidence chain before and after.
- The persisted Daily Brief remains schema `daily_decision_brief.v1`; new fields are additive and old revisions normalize safely.

## Non-goals

- No second candidate lifecycle, delivery pointer, receipt, renderer, scheduler, or notification sender.
- No Action Timeline, Timeline CLI/Markdown, EOD Planning View, generic market-news stream, auto-ranking, auto-cancellation, or auto-trading.
- No change to candidate identity, action ID fields, candidate ranking, labeled-only authority, capacity calculation, or existing event-risk warn/reject filtering.
- No production config mutation, service mutation, real notification, or remote upgrade in this work unit.

## Parsimony Decision

The implementation will reuse the run snapshot, opening action identity, Daily Brief repository, domain diff, and renderer. The only new domain concept is a candidate-bound event-risk projection required to express evidence semantics safely. It will not introduce a new store, state machine, CLI, config section, or publication workflow.

## Blocking Open Questions

None after user confirmation. Provider-neutral occurrence matching and completeness normalization are implementation-plan decisions and must fail closed when evidence is insufficient.

## Residual Risks

- Provider APIs may not expose a durable corporate-event occurrence identifier. The implementation must keep a stable event-series key and use conservative occurrence matching; ambiguous matches degrade instead of fabricating a date change.
  - Classification: covered by approved implementation slices A and C.
- Live provider behavior is not exercised without operator authorization.
  - Classification: assigned to later authorized release/production canary work.

## Completion Status

Goal confirmation passed. Next gate: implementation plan.
