# Gateflow Implementation — Sell Put Top1 W5 Evaluator S1

- Gate: `implementation`
- Work unit: `sell-put-top1-w5-evaluator`
- Slice: `S1 — pure evaluator and seams`
- Artifact path: `docs/gateflow/sell-put-top1-w5-evaluator/implementation-s1.md`
- Status: `implementation and slice code review complete`

## Scope and outcome

Implemented the approved evaluator-only slice. `evaluate_research()` now consumes an exact materialized W4 dataset envelope, exact-expiration close receipts, and the existing HK fee contract; re-ranks every authorized arm over the same frozen accepted set; delegates economics and paired statistics to W1B; and returns one compact, unsealed deterministic evaluation.

The implementation adds no runner, provider, storage schema, scheduler, CLI, Agent tool, lifecycle transition, production configuration, notification, release, or deployment path.

## Changed files

- `src/application/strategy_lab/top1/research.py`
  - exact envelope, W4 manifest, projection, receipt, and fee validation;
  - same-universe baseline/challenger evaluation;
  - fail-closed close/currency/economics handling;
  - fixed 40-day statistics policy and deterministic leader selection;
  - compact unsealed output.
- `tests/test_strategy_lab_top1_research.py`
  - 40-day and multi-point calculation, evidence failures, deterministic selection, tamper rejection, real W4 materialization, source deletion, and M3 authorization seam.
- `tests/test_strategy_lab_top1_architecture.py`
  - pure-owner allowlist and production tick exclusion.
- `docs/DEPENDENCY_GRAPH.md`, `docs/dependency_graph.mmd`
  - regenerated import graph; zero production cycles.

## Implementation decisions

- Reused W1A ranking, W1B economics/statistics, W4 schema identity, and M3 lifecycle without adding parallel policy owners.
- A differing Top1 with any non-HKD candidate, missing/conflicting/unavailable close, or unavailable assignment fee blocks the whole evaluation before statistics.
- Same or both-empty Top1 does not require close or fee-plan facts.
- Current real profiles can yield at most one passing changed arm under the mandatory concentration non-increase gate. The future-safe multi-pass ordering is therefore tested with synthetic W1B pass summaries; real statistics behavior remains covered by W1B and the end-to-end unique-leader test.
- The evaluation is deliberately unsealed. Publication and revision ownership remain outside this slice.

## Validation

- `pytest` focused W5: `14 passed`.
- W5 + W1B + W4 + M3/store + architecture: `55 passed`.
- Ruff on changed Python files: passed.
- Dependency graph check: passed; `production_modules=590`, `cycles=0`.
- `git diff --check`: passed.
- `basedpyright`: attempted but unavailable in the existing project environment (`No module named basedpyright`). No dependency was installed for this slice.
- Kimi code review and evidence-led re-review: passed with no unresolved blocker/high/medium/low finding; review artifact: `docs/reviews/code-review-20260815-161754.md`.

## Documentation decision

No product/operator documentation changed because this slice exposes no public command or runtime capability. Gateflow artifacts and the generated dependency graph are the only documentation updates.

## Residual risks and uncovered areas

- Real close/quota/calendar/fee-plan acquisition and sealed receipt publication: assigned to the remaining W5 runner work after W0R is green.
- Provider retry/dedupe/capacity behavior: assigned to the remaining W5 runner and W7 readiness work.
- Real 40-day strategy conclusion: assigned to a separately authorized pilot; synthetic evidence is not a strategy conclusion.
- Static type analysis: assigned to project toolchain/CI because `basedpyright` is not installed locally; focused runtime and lint checks passed.
- No release, deployment, provider request, live experiment, or production write occurred.

## Completion signal

The approved S1 behavior is implemented, locally verified, and slice-reviewed. The next Gateflow entry point is the accepted-slice commit followed by aggregate review.
