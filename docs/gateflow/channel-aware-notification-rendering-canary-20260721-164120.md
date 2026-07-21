# Gateflow Live Canary — Channel-aware Notification Rendering

## Gate

- Work unit: `channel-aware-notification-rendering`
- Slice: `Slice 5 — Explicitly approved canary and rollout`
- Gate: `live canary acceptance`
- Decision: `superseded by later operator mobile-readability feedback; needs-fix`
- Executed on: `2026-07-21` (Asia/Shanghai)
- Branch: `feat/channel-aware-notification-rendering`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/108`
- Artifact path: `docs/gateflow/channel-aware-notification-rendering-canary-20260721-164120.md`

## Post-canary Qualification Withdrawal

- The delivery/API evidence in this artifact remains valid: all five messages were accepted as `post`, read back as one exact `md` node, and matched the sent Markdown hashes.
- After reviewing the notification family as a whole, the operator reported that two-level indentation is difficult to read on Feishu mobile and requested a flat, unified presentation.
- That later product feedback supersedes the initial manual visual acceptance. Slice 5 visual qualification is withdrawn and the current rollout status is `needs-fix` pending renderer changes and a separately authorized re-canary.
- The Draft PR was unmerged and unreleased when the issue was raised. No production text rollback or duplicate replacement send is required.
- No new live canary is authorized by this correction; a future five-category re-canary still requires explicit operator approval.

## Authorization and Safety Boundary

- The operator explicitly authorized sequential live canaries for all five approved categories.
- Each canary used synthetic, clearly labeled content derived from the real renderer fixtures and a distinct canary UUID.
- The five categories were evaluated in order. No category failed, so the controlled rollback path was not triggered.
- No text replacement was sent for any confirmed post canary.
- No near-28-KiB live canary was sent because that additional live boundary test was not authorized; deterministic tests remain the evidence for the size boundary.
- Recipient evidence is limited to the redacted open-ID suffix `5c7e58`; credentials and the full recipient identifier are not stored in this artifact.

## Delivery and Readback Evidence

| # | Category | Canary UUID | Message ID | Request bytes | Markdown chars | Markdown SHA-256 | HTTP/API result |
|---:|---|---|---|---:|---:|---|---|
| 1 | Daily Brief | `om-canary-20260721-daily-brief-160405` | `om_x100b6ac631a99094b49f107d5514e74` | 946 | 428 | `0f933da2588187ef09112f0ad86158956d14d1ab0776fc09c106c4bffe4b4894` | first attempt; HTTP 200; Feishu `code=0` |
| 2 | Compact Tick | `om-canary-20260721-2-083714` | `om_x100b6ac6b56218a0b2193d4eb02c445` | 751 | 360 | `618d2dd6feac1dc76856c2a6a29b04a56cc926e6560673e283a100b8460d9bb5` | first attempt; HTTP 200; Feishu `code=0` |
| 3 | Trade Receipt | `om-canary-20260721-3-083715` | `om_x100b6ac6b50e8880b25203c8524299a` | 848 | 360 | `2cdb8c3a3c2c0a59a10fc99d4fec97b77d151b0f1f14f6fbdf00597f32e7d1ae` | first attempt; HTTP 200; Feishu `code=0` |
| 4 | Maintenance Receipt | `om-canary-20260721-4-083716` | `om_x100b6ac6b518ec4cb1f83b4c16d57d1` | 708 | 314 | `a50026bf458f434181af868592ec50e9ff2ce67e084d096355f0a2b333d40e02` | first attempt; HTTP 200; Feishu `code=0` |
| 5 | Failure/Recovery | `om-canary-20260721-5-083717` | `om_x100b6ac6b52a58a8b255bc93ca26ca2` | 551 | 237 | `3e9e252d3631db8c3fe03acb243c668e4ba4a912b1ea77ed029c447aa4c34c4c` | first attempt; HTTP 200; Feishu `code=0` |

All five readbacks verified:

- `msg_type=post`;
- one `content_v2` paragraph containing exactly one `md` node;
- empty post title;
- `deleted=false` and `updated=false`;
- readback Markdown character count and SHA-256 exactly matched the sent canonical Markdown;
- request body size was below the fixed 28 KiB (`28,672` byte) budget.

## Visual Acceptance

- Daily Brief: the operator visually accepted the delivered canary before authorizing the remaining sequence.
- Compact Tick, Trade Receipt, Maintenance Receipt, and Failure/Recovery: after all four were delivered and read back, the operator explicitly confirmed `第2到5条通过` in response to the desktop-and-mobile visual acceptance request.
- Initial result at canary time: all five categories were manually accepted.
- Current interpretation: the later mobile-readability feedback supersedes that acceptance; the transport passed, but the renderer presentation requires correction.
- Evidence form: manual operator confirmation. Automated desktop screenshots were not captured because the available UI inspection path required broader system screen-recording access that was not approved; that privacy boundary was not bypassed. Mobile rendering necessarily remained operator-confirmed.

## Validation and Change Scope

- Live delivery/readback: passed for all five categories.
- Initial visual acceptance: recorded for all five categories, then superseded by later operator feedback.
- Rollback: not required.
- Production code/tests changed during canary execution: none.
- Runtime configuration changed: none.
- Persistent canary scripts or payload files added: none; temporary canary helpers/evidence remained under `/tmp`.
- Broad test rerun: not required for this docs-only evidence commit; the pre-canary implementation closeout already recorded `205 passed`, Ruff, compileall, `git diff --check`, deepreview, PR review, and passing GitHub checks.

## Residual Risks / Uncovered Areas

1. Near-limit real-provider behavior was not exercised with a live message.
   - Classification: operator-owned optional follow-up; deterministic byte-boundary tests cover the enforced runtime contract.
2. No automated screenshot artifact is stored.
   - Classification: accepted evidence limitation; explicit operator desktop/mobile confirmation supplies the product acceptance decision without expanding screen-recording access.
3. Future Feishu client rendering may change independently of this implementation.
   - Classification: operational monitoring concern; use the documented controlled rollback if a future regression is observed.

The mobile-readability issue now blocks rollout qualification until the flat renderer fix is validated and explicitly re-canary-tested.

## Completion Status and Next Entry Point

- Slice 5 status: `needs-fix`.
- Feishu `post` + single `md` node transport: qualified by live API and exact readback.
- Renderer visual qualification: withdrawn pending the mobile-flat fix and a separately authorized five-category re-canary.
- Controlled text rollback: not triggered because the PR remained unmerged and unreleased; if flat Post still fails the next acceptance gate, use the documented code/version rollback to text.
- Next entry point: complete deterministic validation and review of the mobile-flat fix, then request explicit authorization before any new live canary. No merge, release, deployment, or live send is authorized by this artifact.
