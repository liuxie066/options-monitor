# Implementation — Daily Decision Brief S2

- **Gate**: implementation
- **Work unit**: `daily-decision-brief`
- **Slice**: S2 — structured assembler and persistence lifecycle
- **Date**: 2026-07-19
- **Selected base**: accepted S1 commit `0c78fbfb`
- **Status**: implementation complete; pending code review
- **Artifact path**: `docs/gateflow/daily-decision-brief-s2-implementation-20260719.md`

## Changed files

- `src/application/daily_decision_brief_repository.py`
- `src/application/daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_repository.py`
- `tests/test_daily_decision_brief_service.py`
- `docs/DEPENDENCY_GRAPH.md` (generated review fix)
- `docs/dependency_graph.mmd` (generated review fix)

## Decisions

- Repository uses one `fcntl.flock` per `account + market` and a separate shared-current-index lock.
- Revision identity is `market + market_trading_date + account`; same-day revisions increment, new trading dates reset to revision 0.
- Run-scoped brief/diff filenames are market-qualified so a single `--market-config all` run preserves both US and HK immutable envelopes.
- Every prepare persists revision/current/run-scoped brief/shared index/run-scoped diff before returning a lifecycle object.
- Delivery is `full` until a full brief is confirmed; subsequent diffs compare to the last confirmed revision; quiet/no-send/failure have no pointer mutation path.
- Full delivery keys include a semantic brief digest: same semantic content retries with the same key, while changed canonical content receives a new key. The immutable run diff stores that digest for confirmation-time verification.
- Confirmed delivery is monotonic and stale completion cannot overwrite a newer pointer.
- Read APIs return explicit `available/reason` results for missing or incompatible state.
- Assembler reads only structured run artifacts. Sell Put and Covered Call call canonical `rank_candidate_rows`; Combo Yield preserves emitted pair order and group/leg identity.
- Candidate duplicates are removed explicitly before domain normalization. Partial CSV/prefetch failures become `data_gaps`; pipeline failure, all decision sources unavailable, or all required capacity facts unavailable becomes account-wide `blocked`.
- Source artifacts record run-relative paths and market-qualified row counts; notification Markdown is not read.

## Validation

- `python3 -m pytest -q tests/test_daily_decision_brief_repository.py` -> `15 passed`.
- `python3 -m pytest -q tests/test_daily_decision_brief_service.py` -> `11 passed`.
- `python3 -m pytest -q tests/test_daily_decision_brief_domain.py tests/test_daily_decision_brief_repository.py tests/test_daily_decision_brief_service.py` -> `40 passed`.
- `python3 -m compileall -q domain/domain/daily_decision_brief.py src/application/daily_decision_brief_repository.py src/application/daily_decision_brief_service.py tests/test_daily_decision_brief_domain.py tests/test_daily_decision_brief_repository.py tests/test_daily_decision_brief_service.py` -> passed.
- `git diff --check` -> passed.

## Docs decision

No public behavior docs in S2; CLI, Agent Tool, config and handbook updates remain owned by approved S4. Generated dependency graph artifacts were refreshed because the focused dependency-graph regression otherwise failed.

## Residual risks / uncovered areas

- Renderer, notification routing, provider idempotency override and confirmed-delivery wiring are covered by approved S3.
- Public read surfaces and default-off config are covered by approved S4.
- Cross-module notification/tick/config regressions are covered by approved S5.
- Provider support for idempotency remains a later production-observation risk already classified by the accepted plan.
- No unclassified residual risk at implementation entry to code review.

## Gate transition

- **Current gate**: S2 code review
- **Next entry point**: run `deepreview` against the S2 workspace diff from `0c78fbfb`, classify findings, then fix/re-review.
