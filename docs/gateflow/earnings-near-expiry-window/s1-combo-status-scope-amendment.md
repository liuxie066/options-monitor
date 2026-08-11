# Gateflow Scope Amendment — S1 Combo Funding Put Status Propagation

- Work unit: `earnings-near-expiry-window`
- Slice: `S1`
- Gate: `code review fix`
- Date: 2026-08-11
- Status: accepted as the minimum fix for DeepReview finding `CR-S1-01`
- Review artifact: `docs/reviews/code-review-20260811-214658.md`
- Artifact path: `docs/gateflow/earnings-near-expiry-window/s1-combo-status-scope-amendment.md`

## Trigger

S1 DeepReview proved that Combo Funding Put uses the corrected six-day Candidate Engine gate but drops the
resulting evidence-completeness status before sealing the Combo JSON snapshot. A hard-window OpenD gap therefore
fails closed at contract filtering while the account-run artifact can still be labelled as a clean `no_candidate`.
This violates the accepted plan's SP/CC/Combo status-parity acceptance criterion.

## Exact scope addition

Allow only these existing production owners in S1:

- `src/application/symbol_monitoring.py` — consume the evidence-driven status returned by the existing Combo step
  instead of hard-coding `completed` for `sp_lc`;
- `src/application/pipeline_watchlist.py` — preserve `completed` plus `partial_data` while aggregating per-symbol
  Combo capture statuses into `combo_yield_candidate_snapshot.json`.

Allow their direct regression tests:

- `tests/test_symbol_monitoring_fetch_spec_merge.py`;
- `tests/test_pipeline_capture_status_routing.py`.

`src/application/daily_decision_brief_service.py` and `tests/test_daily_decision_brief_service.py` were already in
the accepted S1 scope. They will be used to make the existing JSON consumer respect the sealed Combo
`opening_status` when the optional strategy-status index is absent.

## Boundaries

- No new status enum, snapshot schema, strategy, policy window, CSV format, or public command is introduced.
- The fix reuses the existing `completed/partial_data`, `unavailable/data_unavailable`, and Combo snapshot statuses.
- Combo candidate JSON remains the formal authority; CSV remains audit-only and is not read back for eligibility.
- Legacy `inline` presentation behavior is not expanded; current configured `separate` mode receives the formal
  status handoff. General legacy-mode retirement remains outside this work unit.
- No configuration write, live OpenD request, production tick, notification, release, deployment, or remote upgrade.

## Required verification

- Pure hard-window evidence gap with zero pairs seals/loads as unavailable, never clean `no_candidate`.
- A valid pair plus an unresolved sibling remains present and seals as `partial_data`.
- Earnings evidence gap plus an independent definitive reject remains a lawful clean outcome.
- Existing Combo capture aggregation and Daily Brief tests remain green.
