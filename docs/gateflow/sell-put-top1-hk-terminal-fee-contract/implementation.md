# Gateflow Implementation — Sell Put Top1 HK Terminal Fee Contract

- Gate: `implementation`
- Work unit: `sell-put-top1-hk-terminal-fee-contract`
- Accepted plan commit: `8b879390`
- Branch: `feat/sell-put-top1-hk-terminal-fee-contract`
- Status: implementation and slice review pass; next entry point `accepted slice commit`

## Implemented scope

- `calc_futu_hk_terminal_fee()` is the versioned HK assignment/exercise/expired-worthless entry point and reuses the existing seven-component HK stock arithmetic.
- The v1 result has one exact key set, HKD/six-decimal component invariants, strict positive-integral and finite-value checks, and a named audit-only standard-fixed estimate.
- Actual broker fee evidence remains first authority.
- Existing assigned-stock and portfolio-assignment consumers no longer treat incomplete HK terminal fees as usable net economics.
- Ordinary event/position `account_fee_plan` mappings are deliberately ignored; only a future separately validated receipt owner may pass a plan to the pure calculator.
- HK expired-worthless now carries an explicit zero-fee source/version/reason.
- Missing lifecycle fees suppress net PnL and annualized efficiency.
- Missing row-level net economics now also suppress the corresponding lifecycle aggregate net and annualized efficiency instead of being counted as zero.
- Dependency graph outputs were mechanically regenerated under the recorded scope amendment; production modules remain `577` with zero cycles.

## Validation evidence

- Focused fee/consumer tests: `45 passed`.
- Adjacent money-path and Strategy Lab regressions: `329 passed`.
- Full repository in sandbox: `4754 passed, 10 skipped, 1 failed, 5 warnings`.
  - The only failure was sandbox denial of a loopback socket bind.
  - The exact HTTP test passed outside sandbox: `1 passed`.
- Ruff lint over all six production/test files: pass.
- Dependency graph check: pass, `production_modules=577`, `cycles=0`.
- `git diff --check`: pass.
- Whole-file Ruff format was not applied because five pre-existing large files would receive unrelated formatting churn.

## Review evidence

- Initial Kimi DeepReview: `docs/reviews/code-review-20260815-034135.md` — fail with two findings.
- Fix decisions: `docs/gateflow/sell-put-top1-hk-terminal-fee-contract/code-review-fix.md`.
- `DR-HKF-02` was fixed; independent review also found and fixed aggregate fail-closed defect `ROOT-HKF-01`.
- `DR-HKF-01` was rejected with a direct reproduction: current source returns `amount=2.0000000000000064e+16` and the component-sum invariant is true.
- `docs/reviews/code-review-20260815-034739.md` is retained only as a superseded audit artifact because it contained factual symbol/test-reference errors.
- Corrected Kimi re-review: `docs/reviews/code-review-20260815-035048.md` — pass with zero unresolved findings.

## Scope evidence

- Primary files are the exact three production, three test, and one preflight files in the accepted plan.
- `docs/DEPENDENCY_GRAPH.md` was added by `scope-amendment-dependency-graph.md`; `docs/dependency_graph.mmd` remained unchanged after generation.
- No provider/OpenD call, runtime/config mutation, notification, trade, ledger write, service change, release, deployment, or real experiment occurred.

## Docs decision

- Updated `docs/performance/sell-put-top1-capability-preflight-20260814.md` to distinguish a locked source fee contract from runtime readiness.
- Added Gateflow and review artifacts for the work unit.
- No CLI or runtime operations documentation changed because this slice adds no CLI, provider, config, or runtime surface.

## Review boundary

Kimi DeepReview must compare implementation against accepted plan `8b879390`, including:

- exact v1 result contract and component arithmetic;
- numeric trust-boundary behavior;
- actual-fee precedence and ordinary payload plan-injection rejection;
- both consumer fail-closed paths and net-output suppression;
- official Futu fee semantics and source/version provenance;
- overengineering/duplicate formula/import-boundary risks;
- tests that may merely mirror implementation instead of proving the money-path contract.

## Residual risks and owners

- Real `lx` fee-plan receipt and validated receipt intake: assigned to the later W0R/provider work unit; they continue to block provider-dependent research and a real pilot.
- OpenD, quota, calendar, K-line, observation, and terms-capacity gaps: assigned to their existing later W0R work; unchanged by this source contract.
- Full-suite loopback failure: classified as a sandbox-only environment limitation and covered in this slice by the exact test passing outside sandbox.
- No accepted or deferred code-review finding remains in this slice.
