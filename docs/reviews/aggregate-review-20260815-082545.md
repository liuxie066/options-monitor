# Aggregate Deep Review

## Scope

- Mode: aggregate review of branch commits, read-only
- Branch: `feat/sell-put-top1-w5`
- Range: `origin/main@6b16fb3d..HEAD` (`82e29ac6` accepted plan + `cf54f979` evaluator slice)
- Output file: `docs/reviews/aggregate-review-20260815-082545.md`
- Included scope: accepted W5 evaluator-only plan, `evaluate_research()` implementation, W4/M3 seam tests, W5 architecture assertions, regenerated dependency graph, and Gateflow artifacts
- Excluded scope (accepted deferrals, not findings): provider runner, real close/quota/calendar acquisition, receipt sealing/publication, storage schema, real 40-day experiment, W6 validation, CLI/Agent tool, release, deploy
- Review question: does the slice accurately implement the accepted plan, hold the trust boundary (determinism, fail-closed, statistics/economics contracts, W4/M3 seams), keep dependency direction, avoid over-design and goal drift, and avoid claims exceeding evidence

## Findings

No blocker, high, medium, or low finding.

Every candidate below was investigated against source and rejected with evidence.

### AR-01 — rejected-with-reason — output omits `point_results` but remains recomputable

- Candidate: the variant result drops per-point economics and statistics `point_results`, which could break auditability of the daily deltas.
- Evidence: `plan.md` §7 explicitly fixes the compact output contract (`variant_result` exact keys; "omits duplicated candidate rows, point-level economics, raw provider payloads, and statistics `point_results`"). Recomputability is preserved because the W4 projections and close/fee facts remain the audit facts, and each daily delta item keeps `trading_date`, `effective_point_count`, and `daily_delta`. `tests/test_strategy_lab_top1_research.py:80-123` locks the exact top-level and variant key sets, and `test_selects_unique_leader_and_aggregates_two_points_by_day` re-derives the two-point daily mean by hand.
- Decision: rejected. The compaction is the accepted contract, not drift.

### AR-02 — rejected-with-reason — hard-coding `required_days=40` and the fixed policy is the versioned contract

- Candidate: `research.py` fixes `required_days: 40`, `confidence_level: 0.95`, `worst_fraction: 0.20` instead of reading them from the spec, which could silently diverge if the spec changes.
- Evidence: `plan.md` §2 fixes these as non-configurable for v1; `contracts._validate_research_evaluation()` (`contracts.py:312-346`) hard-fails any spec whose `required_days` is not 40, and `validate_experiment_spec()` runs before any evaluation, so an out-of-contract spec can never reach the fixed policy. The architecture test also pins the research module's import allowlist.
- Decision: rejected. The spec validator is the guard; duplicating the values in the evaluator is consistent with the fixed v1 contract, not a drift channel.

### AR-03 — rejected-with-reason — `candidate_currency_mismatch` blocks the whole window before statistics

- Candidate: one non-HKD compared candidate makes the entire research window `insufficient_evidence` instead of degrading only the affected variant, which could be seen as over-broad.
- Evidence: this is the accepted fail-closed rule in `plan.md` §6 step 5 ("fail the entire research evaluation ... This prevents cross-currency economics and selection from a partially evaluable set of levels") and §9. The economics owner (`economics.calculate_expiry_efficiency`) is HKD-only via `calc_futu_hk_terminal_fee`, and `ranking.validate_ranking_projection()` requires uppercase currency but does not restrict to HKD, so the evaluator is the correct boundary to stop cross-currency comparisons. `test_currency_and_materialization_tampering_fail_closed` proves economics is never called for a USD candidate (monkeypatched to `pytest.fail`).
- Decision: rejected. Whole-window fail-closed is the designed behavior; partial-window selection would be the defect.

### AR-04 — rejected-with-reason — unchecked W4 manifest fields are bound by the content hash

- Candidate: `_validate_dataset()` does not cross-check `cutoff_trading_date`, `window_facts_content_sha256`, `market_calendar_ref`, `market_calendar_sha256`, `trading_calendar_dates_sha256`, `maturity_evidence_ref`, or `maturity_evidence_sha256` against the spec, so a tampered manifest field could pass.
- Evidence: `research.py:246-254` recomputes `canonical_sha256` over every manifest field except `content_sha256` and compares it to the supplied `content_sha256`, then recomputes the canonical file SHA-256 over the full manifest and compares it to the spec-pinned `research_source.dataset_sha256`. Any tampered field breaks the semantic hash and raises `research_corpus_conflict` fail-closed (covered by `test_currency_and_materialization_tampering_fail_closed`, which flips `cutoff_trading_date` and expects the error). The fields the evaluator actually consumes for evaluation semantics (`market`, `account`, `cutoff_at_utc`, `market_calendar_version`, 40 ordered dates, start/end) are additionally cross-checked against the spec. Refs (`market_calendar_ref`, `maturity_evidence_ref`) and evidence hashes are provider/runner-side facts whose verification the accepted plan assigns to the W4 freeze path and the future runner; the pure evaluator cannot read those files by design (plan §2, §5.1).
- Decision: rejected. Hash binding plus consumed-field cross-checks cover the trust boundary; verifying unused evidence refs here would require filesystem I/O that the plan explicitly forbids in this slice.

### AR-05 — rejected-with-reason — the M3 seam's synthetic receipt hash is not a false sealing claim

- Candidate: the M3 seam test passes the research generation terminal hash as `research_receipt_file_sha256`, which could be read as claiming the evaluator payload was sealed and bound into the lifecycle.
- Evidence: `plan.md` §8 fixes the seam scope ("The M3 seam does not claim the evaluation payload itself has been sealed. It supplies synthetic receipt ref/hash solely to exercise the already implemented M3 boundary."). The test asserts exactly two things: a challenger different from the evaluator's `leader_variant_id` is rejected, and the accepted lock remains `validation_authorization_status=unconfirmed` so `start_validation` raises `authorization_required`. The implementation artifact states the payload is unsealed and the completion signal forbids calling the original W5 complete. `lifecycle.lock_challenger()` only validates ref/hash shape (`lifecycle.py:715-717`); real receipt binding belongs to the deferred runner slice.
- Decision: rejected. The seam tests the M3 gate, not sealing; no artifact claims sealing happened.

### AR-06 — rejected-with-reason — `account_fee_plan=None` fails closed through the W1B owner

- Candidate: a null fee plan could be defaulted into zero fees and permit leader selection.
- Evidence: `economics.calculate_expiry_efficiency()` passes the plan unchanged to `calc_futu_hk_terminal_fee()`, which converts a non-dict plan to an empty fact set, keeps `complete=false`, and returns `hk_account_fee_plan_missing`; the economics layer then returns `not_evaluable / required_outcome_missing / expiry_fee_unavailable`, and the evaluator turns any not-evaluable result into whole-window `insufficient_evidence` with no leader. `test_incomplete_assignment_fee_and_short_window_fail_closed` exercises the exact `None` plan input and asserts `insufficient_evidence / required_outcome_missing / expiry_fee_unavailable`. The evaluator never defaults the plan (`research.py:441-443` returns `None` unchanged).
- Decision: rejected. Fail-closed behavior is implemented and regression-locked; this was already dispositioned as CR-03 in the slice review and the aggregate review independently re-verified it against source.

### AR-07 — rejected-with-reason — dependency direction and producer isolation hold

- Candidate: the new module could leak into the Candidate Engine / tick path or import forbidden owners.
- Evidence: `research.py` imports only `hashlib`, `math`, `re`, `collections.abc`, `datetime`, `typing`, `domain.domain.decision_state_fingerprint`, `domain.domain.fee_calc`, `src.application.shadow_replay.common`, and the four top1 owners (contracts, corpus schema constant, economics, ranking, statistics). `tests/test_strategy_lab_top1_architecture.py` asserts this exact allowlist and additionally asserts no production tick module imports `research`, `lifecycle`, `corpus`, or the experiment store. The regenerated graph reports `production_modules=590 cycles=0`, and `generate_dependency_graph.py --check` passes.
- Decision: rejected. The direction matches `plan.md` §3.

### AR-08 — rejected-with-reason — determinism and input-order independence are enforced, not just claimed

- Candidate: receipt ordering or dict iteration could change output.
- Evidence: required close keys are iterated in sorted order (`research.py:624`), missing receipts are sorted by the full tuple (`research.py:519-527`), pre-statistics reason sets are sorted and de-duplicated, variant output order follows the authorized spec order, and leader selection uses the fixed key `(mean desc, lcb desc, worst-tail desc, variant_id asc)`. `test_required_close_failures_block_every_variant_and_order_is_stable` asserts forward vs. reversed receipt input produce byte-identical results across three failure modes, and `test_passing_leader_uses_every_deterministic_tie_break` covers every tie-break key including lexical variant ID last.
- Decision: rejected.

## Open Questions

- None for this slice.

## Residual Risk

- Accepted deferrals stand as documented in `plan.md` §11: real close/quota/calendar/fee-plan acquisition, receipt sealing, provider retry/dedupe, and the real 40-day conclusion belong to the deferred runner and pilot work. Nothing in this slice claims otherwise.
- `basedpyright` is not installed in the project environment, so static type analysis was not run locally; runtime (55 focused tests), Ruff, dependency-graph check, and `git diff --check` all pass. This is a toolchain gap, not a code defect.
- The future runner must use the close-receipt failure-reason vocabulary (`_CLOSE_FAILURE_DETAILS`) defined here; the seam is documented but not yet exercised by a producer, which is expected at this stage.

## Verification run during this review

- `pytest -q tests/test_strategy_lab_top1_research.py tests/test_strategy_lab_top1_w1b.py tests/test_strategy_lab_top1_corpus.py tests/test_strategy_lab_top1_store.py tests/test_strategy_lab_top1_architecture.py -p no:cacheprovider` — 55 passed.
- `ruff check` on the three changed Python files — passed.
- `scripts/generate_dependency_graph.py --check` — current, 590 production modules, 0 cycles.
- `git diff --check origin/main..HEAD` — clean.
