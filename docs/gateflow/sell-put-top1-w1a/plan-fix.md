# Gateflow Fix Artifact — Sell Put Top1 W1A PlanReview

- Gate: `fix`
- Work unit: `sell-put-top1-w1a`
- Review artifact: `docs/reviews/plan-review-20260815-015110.md`
- Fixed target: `docs/gateflow/sell-put-top1-w1a/plan.md`
- Artifact path: `docs/gateflow/sell-put-top1-w1a/plan-fix.md`
- Status: `fix complete; pending re-review`

## Finding decisions and fixes

### PR-W1A-01 — accepted — fixed

- Removed the proposed top-level `content_sha256` and second canonical hash path.
- Required `research_artifact_provenance.v1` with `artifact_kind=sell_put_ranking_projection`.
- Required reuse of the existing `attach_artifact_provenance()`, `validate_artifact_provenance()`, and canonical provenance content hash.
- Bound reranking results to `artifact_provenance.content_sha256`.
- Added missing/wrong provenance kind/hash/source-generation failure assertions to the implementation plan.

Final status: `已修复`.

### PR-W1A-02 — accepted — fixed

- Replaced three unrelated scalar inputs with one exact `point_binding` contract.
- Fixed point ID, snapshot hash, and source commit hash lengths.
- Required safe relative POSIX snapshot refs and rejected absolute/traversal paths.
- Required point `market/account/run_id/opening_snapshot_sha256` to match the sealed snapshot.
- Kept W2 as the sole recommendation-point publisher; W1A only validates the minimal binding needed to construct the projection.
- Added mismatch and path-safety failure assertions to the implementation plan.

Final status: `已修复`.

## Validation

- Plan and parent contracts re-read after edits.
- `git diff --check` pending with re-review.

## Residual risks

- Provider/account runtime gaps: covered by W0R.
- Formal producer point publication and write-once semantics: covered by W2; W1A freezes only the input binding required by the projection.
- Corpus persistence and final file-byte hash: covered by W4 using the same provenance/write contract, so no translation seam remains.

## Completion status

`fix complete`; next entry point: `plan re-review`.
