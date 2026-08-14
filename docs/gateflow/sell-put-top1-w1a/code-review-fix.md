# Gateflow Fix — Sell Put Top1 W1A Code Review

- Gate: `fix`
- Work unit: `sell-put-top1-w1a`
- Source review: `docs/reviews/code-review-20260815-022023.md`
- Final re-review: `docs/reviews/code-review-20260815-023230.md`
- Status: complete; final Kimi re-review found no unresolved finding

## Finding decision

### DR-W1A-01 — rejected as false positive

The review claims that a null-return row can sort ahead of a known-return row
under `current_tie_break`. `_rank_return_bands()` does not take that path:

- while any known period return remains, lines 1166-1196 build and emit only a
  non-null return band;
- null-return rows stay in `remaining` until every known-return row has been
  emitted;
- `tie_key` sorts null-return rows only after `usable` becomes empty.

No Candidate Engine production behavior was changed. A focused regression now
gives the null-return row strictly better concentration and assignment-margin
tie keys and still proves the order `KNOWN_RETURN`, `NULL_RETURN`.

## Verification

- `tests/test_candidate_engine_contract.py::test_current_tie_break_ranks_known_return_before_null_return`
- Candidate Engine contract suite: `26 passed`

### DR-W1A-02 — accepted and fixed

The re-review found that an empty ranking projection could previously collapse
lawful `no_candidate` and evidence-insufficient `partial_data` /
`data_unavailable` into the same payload. The builder now consumes the already
validated Sell Put strategy result and requires exactly one matching status:

- non-empty accepted set: `candidates_found`;
- empty accepted set: `no_candidate`.

Any missing, partial, unavailable, or otherwise mismatched Sell Put status
fails closed with `ranking_projection_incomplete`. The projection schema and
Candidate Engine are unchanged.

Regression coverage keeps lawful empty projections valid and rejects sealed
empty snapshots for both `partial_data` and `data_unavailable`.

Final focused Gateflow suite: `136 passed`; Ruff, source compilation, and
`git diff --check` passed.
