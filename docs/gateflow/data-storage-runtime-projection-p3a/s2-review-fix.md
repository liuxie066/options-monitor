# Gateflow S2 DeepReview Fix

- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S2`
- Gate: `fix`
- Initial review: `docs/reviews/code-review-20260814-025422.md`
- Status: accepted; aggregate re-review passed with planned later-slice risks

## Finding decisions

### S2-DR-01 — accepted — fixed

`ProjectionTransition` now carries the accumulator's scalar
`previous_close_event_id`. The public adjust fold uses that domain fact instead
of guessing from arbitrary snapshot fields. A bootstrap snapshot may contain a
legacy `last_close_event_id`, but without a real applied close the subsequent
adjust correctly advances `last_action_at` exactly like the full oracle.

### S2-DR-02 — accepted — fixed

`ResumablePublicationState` now keeps only the open baseline for
`auto_close_exp_src` and `auto_close_grace_days` beside each active lot's
complete current public fields. Before applying a close, the fold restores that
two-field baseline, then overlays the current close. This removes stale prior
expire-close values without deleting fields originally present in a bootstrap
snapshot. The baseline is canonical, bounded by active lots, and covered by
serialization round-trip and alias-isolation checks.

## Verification

```text
focused post-fix projection/publisher tests: 34 passed
new focused resumable suite: 17 passed
500 seeded full-oracle/publication sequences: exact
eligible resume tails at every tested prefix: exact
ruff: passed
```

The semantic implementation digest was refreshed to
`6fc3cd7918b66f6072b5f973bb613850eff4d8ed34dbacf6948e4fdffa39a2d6`.
Aggregate re-review evidence:

```text
docs/reviews/code-review-20260814-031800.md: pass-with-risks
```

## Scope discipline

The fix adds one scalar transition fact and two bounded public baseline fields.
It does not retain open payload history, close vectors, checkpoints, or a second
publisher path. No live or production action was performed.
