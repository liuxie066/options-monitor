# Code Review — Daily Decision Brief S1

- **Gate**: code review
- **Work unit**: `daily-decision-brief`
- **Slice**: S1
- **Selected base**: accepted plan commit `94fd3229`
- **Reviewed target**: S1 workspace diff
- **Date**: 2026-07-19
- **Reviewer mode**: `deepreview` current-changes
- **Status**: findings accepted; fix required
- **Artifact path**: `docs/gateflow/daily-decision-brief-s1-code-review-20260719.md`

## Findings

### CR-1 — High — Material diff digest includes mutable display text and breaks crash-retry idempotency

- **Code location**: `domain/domain/daily_decision_brief.py::_canonical_change()` and `_action_change_view()`.
- **Entry point / trigger**: A provider confirms an action-invalidation delta; process crashes before delivery pointer update; next run produces the same material transition but updated `title` or `reason` wording.
- **Expected behavior**: Canonically equivalent material changes reuse the same digest/provider key.
- **Actual behavior**: `_canonical_change()` retains the entire action view, including `title` and `reason`, so copy-only changes produce a new digest.
- **Direct evidence**: `_action_change_view()` includes `title` and `reason`; plan explicitly excludes display text from stable idempotency semantics.
- **Impact**: Feishu can receive a duplicate delta in the post-send/pre-pointer crash window despite stable-key design.
- **Fix direction**: Material digest must project action changes to stable identity/state/priority fields only; renderer may retain title/reason in the non-canonical `changes` payload.
- **Fix risk**: low.
- **Severity**: High.
- **Decision**: accepted.

### CR-2 — Medium — Duplicate stable action identities are silently collapsed during diff

- **Code location**: `normalize_daily_decision_brief()` and `diff_daily_decision_briefs()` action maps.
- **Entry point / trigger**: Assembler emits duplicate rows for the same contract/group identity.
- **Expected behavior**: Contract validation rejects duplicate action IDs or deterministically deduplicates with explicit evidence.
- **Actual behavior**: Normalization allows duplicates; dict comprehension in diff silently keeps the last row.
- **Direct evidence**: `prev_actions = {action_id: item ...}` / `cur_actions = ...` overwrites duplicates.
- **Impact**: Different row order can change priority/state without audit evidence and produce false or missing deltas.
- **Fix direction**: Reject duplicate action IDs during normalization with a clear error. S2 assembler owns explicit upstream deduplication.
- **Fix risk**: low.
- **Severity**: Medium.
- **Decision**: accepted.

### CR-3 — Medium — High-priority downgrade remains silent even though the daily main action materially weakened

- **Code location**: `diff_daily_decision_briefs()` priority transition handling.
- **Entry point / trigger**: Same stable action changes P0 -> P1 or P1 -> P2 while remaining `active`.
- **Expected behavior**: A previously delivered P0/P1 main action losing priority is material.
- **Actual behavior**: Only upgrade-to-P0 is reported; downgrade produces no change.
- **Direct evidence**: Code has `prior != P0 and current == P0`, with no inverse transition.
- **Impact**: Monitoring can stay silent after the main action loses urgency/actionability.
- **Fix direction**: Emit `priority_downgraded` as material whenever a previous P0/P1 moves to a lower priority; retain same-tier rank noise as non-material.
- **Fix risk**: low.
- **Severity**: Medium.
- **Decision**: accepted.

## Open Questions

无。

## Residual Risk

- Recursive NaN/Infinity values in future assembler payloads could make a digest non-standard; classify as covered by current S1 fix because canonical digest is part of this slice.
- No unclassified residual risk.

## Gate decision

- **Decision**: fail pending fix.
- **Current gate**: S1 fix.
- **Next entry point**: fix CR-1..CR-3 plus canonical non-finite JSON handling, add regression tests, re-review.
