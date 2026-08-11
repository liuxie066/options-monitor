# Gateflow Implementation Artifact — Earnings Near-Expiry Window S1

- Work unit: `earnings-near-expiry-window`
- Slice: `S1`
- Gate: `implementation`
- Date: 2026-08-11
- Status: slice implementation, fixes, validation, and clean re-review complete
- Branch: `feat/earnings-near-expiry-window`
- Accepted plan commit: `fa5076f2`
- Artifact path: `docs/gateflow/earnings-near-expiry-window/s1-implementation.md`
- Generated-artifact amendment:
  `docs/gateflow/earnings-near-expiry-window/s1-generated-artifacts-scope-amendment.md`
- Combo-status review-fix amendment:
  `docs/gateflow/earnings-near-expiry-window/s1-combo-status-scope-amendment.md`

## Objective and outcome

Replace the blanket scan-date-to-expiry earnings rejection with one versioned six-natural-day near-expiry rule. Day
0 and day 6 reject; day 7 and farther remain non-blocking context. The same market-local, date-only policy now flows
through Sell Put, Covered Call, and Combo Yield Funding Put. Hard evidence gaps fail closed only unresolved contract
outcomes, while fully evidenced candidates remain eligible with a partial-universe disclosure.

## Changed scope

Production owners:

- `domain/domain/engine/candidate_engine.py`
- `domain/domain/engine/__init__.py`
- `src/application/earnings_calendar.py`
- `src/application/candidate_scanning.py`
- `src/application/sell_put_steps.py`
- `src/application/sell_call_steps.py`
- `src/application/opening_candidate_snapshot.py`
- `src/application/combo_yield_steps.py`
- `src/application/ai_decision_advice/contexts.py`
- `src/application/ai_decision_advice/orchestration.py`
- `src/application/ai_decision_advice/render.py`
- `src/application/ai_decision_advice/prompts/advice/02_boundary.md`
- `src/application/daily_decision_brief_service.py`
- `src/application/daily_decision_brief_renderer.py`
- `src/application/symbol_monitoring.py`
- `src/application/pipeline_watchlist.py`

Contracts/docs/generated artifacts:

- `docs/candidate_strategy.md`
- `docs/STRATEGY_ARCHITECTURE.md`
- `docs/AI_DECISION_ADVICE_DESIGN.md`
- `docs/DEPENDENCY_GRAPH.md`
- `docs/dependency_graph.mmd`
- the two S1 Gateflow artifacts in this directory

Direct tests:

- `tests/test_earnings_calendar.py`
- `tests/test_candidate_engine_contract.py`
- `tests/test_candidate_engine_phase2_contract.py`
- `tests/test_sell_put_strategy_risk.py`
- `tests/test_covered_call_strategy_risk.py`
- `tests/test_sell_put_cash_total_cny.py`
- `tests/test_cc_lp_steps.py`
- `tests/test_combo_yield_steps.py`
- `tests/test_candidate_scanning_evidence.py`
- `tests/test_opening_candidate_snapshot.py`
- `tests/test_ai_decision_advice_orchestration.py`
- `tests/test_ai_decision_advice_render.py`
- `tests/test_daily_decision_brief_service.py`
- `tests/test_symbol_monitoring_fetch_spec_merge.py`
- `tests/test_pipeline_capture_status_routing.py`

## Implementation decisions

1. Candidate Engine owns `EARNINGS_NEAR_EXPIRY_WINDOW_DAYS = 6`, policy version
   `earnings_near_expiry.v1`, strict date classification, and validation of the explicit hard/soft evidence fields.
   Timestamp and `pub_type` remain provenance only; same-market-day events remain pending all day.
2. OpenD earnings evidence is now `opend_earnings_calendar.v2`. The validator proves the exact inclusive scan-date to
   maximum-expiry 1–7-day partition, interval result identities, event source interval, canonical expiration set,
   top-level hash, and exact stored projection before absence can be authoritative.
3. Per expiry, `max(scan_date, expiry-6)..expiry` is the hard window. Earlier scan-to-expiry dates are a soft context
   window. A validated blocker is conclusive even if another hard interval failed; without a blocker, a hard gap is
   unavailable. A soft-only gap never changes eligibility.
4. Candidate outcome aggregation separates diagnostic evidence gaps from eligibility-unresolved outcomes, counts
   each decision exactly once, and verifies accepted-count parity. Put/call strategy status becomes partial only for
   unresolved contracts, not for contracts already definitively rejected by another gate.
5. Existing content-hashed `scope_results` remains the frozen completeness authority. The pure
   `candidate_universe_summary()` projection is consumed by Advice and Daily Brief; it is not persisted as a second
   opening-snapshot truth. Accepted candidates survive an unavailable sibling scope, while zero-candidate unresolved
   input remains Advice-unavailable.
6. Distant events and soft coverage gaps are rendered as non-blocking context. Near blockers, hard unavailability,
   and partial universes have distinct deterministic wording. Presence/list inconsistencies fail closed in Daily
   Brief instead of being rendered as confirmed event/absence.
7. Inspection of Combo Funding Put proved a missing handoff: the scan returned a typed DataFrame, but underwriting
   read the labelled audit CSV back. Lists/booleans in v2 earnings evidence could therefore become strings and be
   rejected as invalid. Formal labelling and underwriting now continue on the typed in-memory DataFrame; the CSVs
   are audit-only. A non-DataFrame scan return fails explicitly rather than falling back to CSV.
8. The unused Daily Brief helper that could read Sell Put/Covered Call candidate CSVs was removed. Current Brief
   candidate authority is `opening_candidate_snapshot.json`; Combo authority is its sealed JSON snapshot. CSV
   remains only in explicitly compatible input/report/history surfaces outside this decision path.
9. Combo scan and underwriting decisions now flow through the same evidence summary/status projection as Sell Put
   and Covered Call. A partial Funding Put universe is preserved through symbol capture, the Combo JSON snapshot,
   and Daily Brief; a sealed JSON status cannot be upgraded by an optional index or candidate CSV.
10. Combo cash processing now enriches every typed Funding Put row but does not filter it before Candidate Engine.
    Candidate Engine remains the sole capacity gate, so a definitive capacity rejection can coexist with an
    auditable earnings evidence gap. The historical `*_cash_filtered.csv` filename remains audit-only.
11. Snapshot collection items fail with `OpeningCandidateSnapshotError` before field access. Candidate-universe
    completeness reuses the snapshot module's clean/benign reason taxonomy and also recognizes outcome-unresolved
    contract scopes, so Advice cannot silently omit a non-benign unavailable sibling.

## Contract and compatibility decisions

- `opening_candidate_snapshot.v1` and its content hash remain unchanged; strategy policy hash input advances to v2
  and binds the earnings policy version/window.
- Old/malformed earnings-calendar artifacts are unavailable for new candidate calculation and are not reinterpreted.
- Historical artifacts are not rewritten.
- Existing reject codes remain stable, with corrected meanings: `risk_earnings_event` is a known 0..6-day blocker;
  `risk_earnings_unavailable` is an unresolved hard-window evidence gap.
- No RV, return, premium, multiplier, fees, delta, liquidity, capital/capacity, or ranking formula changed.

## Validation

- Final affected policy/strategy/snapshot/Advice/Brief set: `265 passed`.
- Final focused completeness/Advice/Brief set: `106 passed`.
- Full repository run: `4717 passed, 10 skipped`; the sole non-product failure was sandbox denial of binding
  `127.0.0.1`, and the exact read-only HTTP test passed outside the sandbox (`1 passed`).
- Ruff over `domain`, `src`, and `tests`: pass.
- `python -m compileall -q domain src`: pass.
- dependency graph: `585` production modules, `0` cycles.
- `git diff --check`: pass.

No live OpenD request, production tick, notification send, config build/write, broker/ledger write, remote command,
release, deployment, or upgrade was run.

## Documentation decision

Updated the strategy contract with the exact 0..6 inclusive natural-day predicate, scan-date versus hard-window
roles, hard/soft failure semantics, same-day policy, and three-strategy parity. Updated Combo architecture to state
that Funding Put calculation remains in memory and audit CSVs are non-authoritative. Updated AI Advice design with
the scope-derived partial-universe disclosure and zero-candidate fail-closed rule. Regenerated dependency artifacts
as required by the repository quality gate.

## Residual risks and uncovered areas

- Live OpenD behavior is represented by deterministic gateway fixtures rather than a production request:
  **assigned to later work unit** or explicit release/upgrade verification because live provider access is outside
  this work unit's authority.
- OpenD only exposes its currently known calendar and no per-symbol completeness guarantee:
  **assigned to later work unit** if the product later requires an additional audited provider or a stronger
  provider contract. This slice uses the confirmed OpenD-only policy, treats successful hard-window intervals as
  the strongest available absence evidence, and documents the remaining provider risk.
- Audit, parsed-market-data, Close Advice, research, and historical/shadow CSV files still exist:
  **assigned to later work unit** if complete CSV retirement is desired. They are outside this opening-candidate
  authority change, and regression coverage proves Combo underwriting plus Advice/Brief no longer rely on candidate
  CSV round-trips.
- The full suite required one sandbox-external localhost test rather than one all-green sandbox invocation:
  **fixed in current slice validation** by isolating the permission-only failure and passing the exact test without
  contacting external or production services.

## Completion status

S1 implementation, finding fixes, validation matrix, and clean DeepReview are complete. The next Gateflow entry
point is the slice commit; no source commit, push, PR, release, deployment, or production action has occurred in
this gate.
