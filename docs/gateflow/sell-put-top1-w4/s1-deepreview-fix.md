# Gateflow Fix — Sell Put Top1 W4 S1 DeepReview

- Gate: `fix`
- Work unit: `sell-put-top1-w4-s1`
- Review artifact: `docs/reviews/code-review-20260815-141531.md`
- Status: all three findings accepted and fixed; pending re-review

## Finding 1 — accepted — fixed

The parent product contract makes the first release `HK/lx` and defers US to a separately confirmed capability expansion. W4 now enforces that same boundary in the Corpus public identity check and both v2 Corpus table constraints. The W4 plan's artifact wording was corrected from `HK|US` to first-release `HK`; a regression proves US is rejected with `corpus_input_invalid` before any Corpus write.

## Finding 2 — accepted — fixed

Every day-seal conflict result now sets the artifact ref/content/file hashes and expected count to `null`. It therefore cannot advertise proposed bytes that were never published or an artifact whose integrity is already in dispute. The regression runs success -> denominator drift -> second different drift and proves all conflict artifact fields remain null while the durable day remains conflict-terminal.

## Finding 3 — accepted — fixed

The public scheduler target-for-date wrapper now normalizes both market and gate `ZoneInfoNotFoundError` to `ValueError`; the Corpus boundary consequently maps them to `CorpusError(corpus_input_invalid)`. The wrapper also rejects non-canonical compact ISO dates. Regressions cover both timezone positions plus the direct wrapper contract and prove an invalid timezone creates no Corpus day row.

## Additional failure coverage

- Feature-off point capture reads the source only to discover identity, then creates no Corpus row/artifact.
- Projection-byte disagreement marks the indexed point conflict and does not overwrite the artifact.
- Missing and content-invalid opening snapshots persist distinct terminal `not_evaluable` reasons.

## Verification

- Focused W1-W4 S1 and adjacent scheduler/producer suite: `116 passed`.
- Ruff over changed production/tests: pass.
- BasedPyright: new `corpus.py` and changed store add zero errors; scheduler remains at the same `22` pre-existing errors as the root-worktree baseline, with no W4-added error.
- `git diff --check`: pass.
