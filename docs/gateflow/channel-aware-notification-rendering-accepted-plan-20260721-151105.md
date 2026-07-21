# Gateflow Accepted Plan — Channel-aware Notification Rendering

## Gate

- Work unit: `channel-aware-notification-rendering`
- Gate: `plan -> plan review -> fix -> re-review`
- Decision: `pass-with-risks`
- Plan: `docs/plans/channel-aware-notification-rendering-plan-20260721.md`
- Initial review: `docs/reviews/plan-review-20260721-142232.md`
- Re-review: `docs/reviews/plan-review-20260721-150356.md`

## Finding Status

- Feishu post 30 KB limit versus text 150 KB: accepted, fixed in the revised plan with exact outer-request UTF-8 byte preflight, 28 KiB fail-closed budget, and structured local diagnostics.
- Daily-Brief-only canary was not representative of the shared adapter blast radius: accepted, fixed with five renderer categories covering Daily Brief, Compact Tick, Trade Receipt, Maintenance Receipt, and Failure/Recovery notices.
- Open accepted findings: none.

## Approved Implementation Boundary

1. Add `send_post_message()` in `src/infrastructure/feishu_bot.py`, preserving existing text/reply behavior.
2. Route proactive Feishu notification delivery through the new sender and normalize local size errors.
3. Preserve local diagnostics in scheduled notification attempts/audits without adding the deterministic size error to retryable codes.
4. Add focused/broader regression tests and update `docs/AGENT_WIKI.md`.
5. Do not execute the live canary/rollout slice without separate explicit authorization.

## Minimality Decision

The accepted solution reuses the current canonical Markdown, Feishu exception hierarchy, delivery adapter, retry state machine, and receipt error contract. It deliberately avoids a renderer registry, DTO/content enum, runtime switch, Markdown parser, truncation, fragmentation, or automatic fallback.

## Validation at This Gate

- Plan review conclusion: `pass-with-risks`.
- No implementation blocker or unclassified residual risk.
- External visual/API risks are assigned to the explicit canary gate.

## Current Gate / Next Entry Point

- Current gate: `accepted plan commit`
- Next entry point after commit: `implementation`
