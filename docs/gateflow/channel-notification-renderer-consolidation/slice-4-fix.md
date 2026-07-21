# Gateflow Slice 4 Fix — Documentation ownership boundary

- Gate: `fix`
- Work unit: `channel-notification-renderer-consolidation`
- Slice: `4`
- Review artifact: `docs/reviews/code-review-20260721-211924.md`
- Status: `fix complete; pending re-review`
- Artifact path: `docs/gateflow/channel-notification-renderer-consolidation/slice-4-fix.md`

## Finding decision

### Finding 1 — accepted — fixed

The implementation had moved System Notice and Receipt presentation ownership into `src/application/notification_shells.py`, but `docs/AGENT_WIKI.md` did not record that owner or the new Receipt family header/type contract. The Slice 4 implementation artifact also incorrectly claimed the existing documentation already covered the shared family.

Fix:

- Added `notification_shells.py` to the Notifications ownership list.
- Documented `# OM · 系统通知 · <component>`.
- Documented `# OM · 回执 · <account>` with `类型｜成交` / `类型｜持仓维护`.
- Explicitly kept OpenD rate limits/recovery, delivery-failure aggregation/retry, trade warnings, and maintenance status/dedupe/persistence with their callers.
- Explicitly prohibited the shell from sending, retrying, reading provider byte limits, or classifying business state.
- Corrected the Slice 4 implementation artifact docs decision.

Final finding status: `已修复`.

## Validation

- `rg` confirms the project manual contains the shared owner, family headers, and caller/shell boundary.
- Focused code and transport tests remain unchanged and are re-run in the re-review gate.
- `git diff --check 528e10d2`: pass.

## Residual risks

- Live client rendering remains outside authorization and is assigned to later authorized canary/deployment evidence.
- Aggregate renderer authority and fallback verification remains covered by approved aggregate validation.
- Legacy strict cleanup remains assigned to the explicitly hard-paused Slice 6.
