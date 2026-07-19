# Fix — Daily Decision Brief S2

- **Gate**: fix
- **Work unit**: `daily-decision-brief`
- **Slice**: S2
- **Target findings**: CR-S2-1 through CR-S2-8
- **Date**: 2026-07-19
- **Status**: fixes complete; pending re-review
- **Artifact path**: `docs/gateflow/daily-decision-brief-s2-fix-20260719.md`

## Fixes

- **CR-S2-1 已修复**：assembler now requires an explicit authoritative `pipeline_succeeded` fact. `AccountResult.ran_scan` is retained only as scan-attempt context and cannot promote a failed pipeline to LIVE.
- **CR-S2-2 已修复**：delivery confirmation loads the immutable run-scoped diff for the target revision and validates exact full/delta kind and provider key. Delta validation also verifies the referenced base revision digest.
- **CR-S2-3 已修复**：delivery pointer field normalization errors are wrapped as `DailyDecisionBriefStateError`; read APIs return structured `state_invalid`.
- **CR-S2-4 已修复**：rejection aggregation filters raw trace/reject rows by canonical symbol market before reusing the existing category/rule classifier.
- **CR-S2-5 已修复**：prepare and latest-read paths verify current against its immutable revision artifact and digest before trusting it.
- **CR-S2-6 已修复**：user-confirmed plan amendment replaces the fixed daily full key with `full:<semantic-brief-digest>`. The digest excludes revision/run/timestamp/display/audit noise, changes when canonical full content changes, is persisted in the immutable run diff, and is revalidated before confirmation.
- **CR-S2-7 已修复**：regenerated `docs/DEPENDENCY_GRAPH.md` and `docs/dependency_graph.mmd` after S1/S2 added three production modules and three focused test modules. The current graph reports 472 production modules and zero cycles.
- **CR-S2-8 已修复**：run-scoped brief/diff filenames are now market-qualified. US and HK prepared under the same production `run_id + account` no longer overwrite each other, and each immutable diff remains independently confirmable.

## Regression coverage

- Pipeline failed with `ran_scan=true` and partial candidates remains `blocked`.
- Wrong full/delta envelope cannot advance delivery state.
- Invalid delivery revision returns structured unavailable.
- Mixed US/HK trace rows are isolated in the HK summary.
- Missing immutable revision makes current unavailable and prevents new revision allocation.
- Same semantic full retry reuses its key; changed full content receives a new key and an old key cannot confirm the newer revision.
- Tampering with the immutable full semantic digest fails closed.
- Generated dependency graph check and its focused regression test pass.
- Same-run US/HK run-scoped brief/diff paths are distinct and both full deliveries confirm against their own immutable envelope.

## Validation

- `python3 -m pytest -q tests/test_daily_decision_brief_domain.py tests/test_daily_decision_brief_repository.py tests/test_daily_decision_brief_service.py` -> `40 passed`.
- `python3 -m compileall -q ...` for S1/S2 production and test files -> passed.
- `python3 -m ruff check ...` for S2 production and test files -> passed.
- `python3 scripts/generate_dependency_graph.py --check` -> `472 production modules, 0 cycles`.
- `python3 -m pytest -q tests/test_dependency_graph_generator.py` -> `2 passed`.
- `git diff --check` -> passed.

## Residual risks

- Multi-file crash-point scenarios remain assigned to approved S5.
- Provider-side idempotency semantics remain subject to S3 review.
- Provider key format/length and real provider idempotency behavior remain covered by approved S3 integration review.
- No unclassified residual risk from CR-S2-1 through CR-S2-8.

## Gate transition

- **Current gate**: S2 re-review
- **Next entry point**: deepreview the fixed S2 target and classify any new findings.
