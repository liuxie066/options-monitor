# Gateflow S1 Implementation — Decision Contract and P0 Parity

- Work unit: `close-advice-strategy-optimization`
- Slice: `S1`
- Status: accepted; deep review passed
- Production behavior: unchanged P0 selection, action, and notification semantics

## Changes

- Added the additive decision contract: `policy_version`,
  `recommendation_state`, `decision_basis`, and
  `decision_evidence_status`.
- Added the pure `p0_current.v1` projection in the domain layer.
- Changed the action-policy registry boundary to consume
  `recommendation_state`; the runner projects current tier/exit facts first, so
  existing action results remain identical.
- Added read-only `legacy_p0` projection for old CSV artifacts.
- Exposed the fields through CSV, `close_advice_read`, its output contract, and
  `close_advice_snapshot`.
- Documented that tier is descriptive evidence and recommendation state is the
  action authority.

## Hidden-consumer inventory

The S1 stop-condition inventory found these remaining legacy consumers. They
are deliberately not changed in S1 because renderer/selector behavior is an S6
promotion concern:

- `close_advice_runner._selected_notify_rows` selects current notifications by
  configured tier.
- `close_advice_runner._append_close_advice_filter_trace` classifies selected
  and ranked rows by tier.
- `daily_decision_brief_service._close_action` derives active/observe state
  from tier and persists `close_action`.
- `daily_decision_brief_renderer._position_status_label` has a tier fallback
  for legacy rows.
- `close_advice_reallocation_shadow` reads formal action/tier as offline input.
- summary/sort/read helpers use tier for presentation ordering and counts; they
  do not execute an action.

No newly added P1/P2/P3 policy is reachable from these production consumers.

## Validation

- Domain/action/analysis: `74 passed, 10 skipped`.
- Runner/contract: `83 passed`.
- Notification/Daily Brief parity: `86 passed`.
- Python compile and `git diff --check`: passed.

## Residual risk and next gate

- The current production selector still treats configured strong/medium tiers
  as notification eligibility. That is intentional P0 parity and must remain
  unchanged until an evidence-backed S6 promotion.
- S2 may add only pure shadow policy variants and must prove they are
  unreachable from production selection.
- Deep review: `docs/reviews/code-review-20260723-010011.md` — no material findings.
- Next gate: accepted S1 commit, then S2 shadow-only policy implementation.
