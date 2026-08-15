# Gateflow Plan — Sell Put Top1 W5 Evaluator Slice

- Gate: `plan`
- Work unit: `sell-put-top1-w5-evaluator`
- Branch: `feat/sell-put-top1-w5`
- Base: `origin/main@6b16fb3d189e68ba9957de3a60cfa59cc89a0294`
- Goal contract: `docs/gateflow/sell-put-top1-w5-evaluator/goal-confirmation.md`
- Design sources:
  - `docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md`
  - `docs/plans/sell-put-top1-modular-technical-implementation-plan-20260814.md`
  - `docs/plans/sell-put-top1-modular-implementation-control-20260814.md`
- Artifact path: `docs/gateflow/sell-put-top1-w5-evaluator/plan.md`
- Current gate: accepted plan; implementation S1 pending

## 1. Goal, motivation, and completion signal

Implement `evaluate_research(dataset, close_receipts, fee_contract)` as a deterministic, side-effect-free application function. It evaluates one authorized research ExperimentSpec against exactly one materialized W4 40-day manifest, compares every non-baseline arm against the baseline on the same accepted candidate universe, and returns an unsealed research-evaluation payload.

Completion requires the assertions in §10, no unresolved accepted review finding, no dependency reversal, and a Kimi aggregate DeepReview pass. Synthetic success proves evaluator correctness only; it does not prove runtime readiness, strategy improvement, a real research receipt, or completion of the full W5 runner.

## 2. Non-goals and fixed boundary

- No `run_research()`, provider call, quota query, retry, dedupe scheduler, filesystem reader/writer, SQLite change, generation revision, terminal projection, or sealed receipt.
- No W4 artifact schema change or public loader. The future runner will read exact bytes, then pass the decoded W4 manifest and projections into this pure boundary.
- No W6 validation logic, live fill observation, outcome job, timer/service, CLI/Agent tool, Prompt/LLM, production config, release, deploy, notification, ledger, or broker action.
- No new ranking, economics, statistics, risk, fee, provider, or repository abstraction.
- No configurable research confidence, tail fraction, or day count: v1 remains fixed at 0.95, 0.20, and 40 by the versioned research-selection contract.

## 3. Existing owners and dependency direction

Reuse:

- `contracts.validate_experiment_spec()` and `build_research_spec_sha256()` for the authorized research contract;
- `corpus.SEALED_HISTORICAL_DATASET_SCHEMA` for the W4 manifest identity only;
- `ranking.validate_ranking_projection()` and `rerank_recommendation_point()` for all arm choices;
- `economics.calculate_expiry_efficiency()` for T0 assumed-fill expiry economics;
- `statistics.summarize_paired_daily_deltas()` for point pairing, day aggregation, t lower bound, tail gate, and concentration gate.

Dependency rule:

```text
research -> contracts + corpus schema constant + ranking + economics + statistics
research -> MUST NOT import lifecycle, store, terminal projection, provider, validation, service, CLI, or LLM
producer/Candidate Engine -> MUST NOT import research
```

The evaluator sets `hard_risk_status=passed` only after consuming a valid W4 accepted-only projection. It does not infer or store a parallel risk model. Generic hard-risk failure behavior remains covered by the existing statistics owner.

## 4. Affected files

Production:

- New `src/application/strategy_lab/top1/research.py`.

Tests/docs:

- New `tests/test_strategy_lab_top1_research.py`.
- Modify `tests/test_strategy_lab_top1_architecture.py` with the W5 import boundary.
- Regenerate `docs/DEPENDENCY_GRAPH.md` and `docs/dependency_graph.mmd` only if the repository generator reports a real graph change.
- Add Gateflow and review artifacts under `docs/gateflow/sell-put-top1-w5-evaluator/` and `docs/reviews/`.

## 5. Pure input contracts

### 5.1 Materialized dataset argument

`dataset` is an exact-key in-memory envelope, not a new stored artifact:

```text
schema_version = sell_put_top1_research_evaluation_input.v1
experiment_spec
dataset_ref
sealed_dataset
ranking_projections = [{projection_ref, projection}, ...]
```

Rules:

- `experiment_spec` must be a valid research-only v1 spec.
- `dataset_ref` and the canonical file SHA-256 of `sealed_dataset` must match `research_source.dataset_ref/dataset_sha256`.
- The manifest must be exact `sealed_historical_dataset.v1`, have a valid semantic content hash, match spec market/account/cutoff/calendar/40-day start/end, and contain exactly 40 ordered unique dates.
- `ranking_projections` must contain exactly one entry for every referenced point and no extra/duplicate ref.
- Every projection must pass W1A validation; ref, semantic content hash, canonical file hash, point ID, market, and account must match the W4 manifest.
- Any baseline/challenger candidate that actually needs an economic comparison must use `currency=HKD`. A different currency produces normal `insufficient_evidence / ranking_projection_incomplete / candidate_currency_mismatch`; W5 does not convert currencies or mix a non-HKD cash basis with the HKD terminal-fee calculator.
- Structural/type/schema misuse raises a stable `ResearchEvaluationError`; artifact/ref/hash disagreement also raises fail closed with the evidence reason code. It never produces a partial winner from a corrupted materialization.

This envelope exists only because a pure function cannot read W4 references or discover an ExperimentSpec. No loader interface or stored envelope is added.

### 5.2 Exact-expiration close receipts

`close_receipts` is a list of exact-key facts:

```text
schema_version = sell_put_top1_research_close_receipt.v1
market, account, stock_owner, expiration
spot_source = opend_history_kline
ktype = K_DAY
autype = NONE
price_field = close
status = available | unavailable
underlier_close
reason_detail
```

An available receipt requires one finite positive close and null reason. An unavailable receipt requires null close and one allowed outcome subreason. A missing required key becomes `research_expiry_close_missing`; duplicate facts for a required `(stock_owner, expiration)` produce normal `insufficient_evidence / required_outcome_missing / expiry_close_receipt_conflict`. No adjacent-date, realtime, adjusted, mid, last, or inferred-price fallback exists. Structurally valid receipts not selected by any differing arm, including duplicate unused keys, are ignored and cannot affect the result.

### 5.3 Fee contract

`fee_contract` has exact keys `market`, `account`, `fee_schedule_version`, and `account_fee_plan`. Identity and schedule must match the ExperimentSpec/current W1B contract. The account plan is passed unchanged to W1B; it is never defaulted. Missing/incomplete valid facts become `required_outcome_missing / expiry_fee_unavailable` only when a differing Top1 actually requires economics.

## 6. Evaluation flow and invariants

1. Validate the input envelope, ExperimentSpec, W4 manifest, all refs/hashes, and every W1A projection before computing any arm.
2. For each point, call baseline profile `current_tie_break`, then every non-baseline variant's authorized `ranking_profile` on the same projection.
3. Same Top1 produces `point_delta=0` without requiring close or fee facts. Both-empty produces no evidence. A one-sided candidate is a contract failure.
4. For each differing Top1, locate both frozen candidate rows, require `currency=HKD`, and collect the unique `(stock_owner, expiration)` close requirements.
5. Before statistics, fail the entire research evaluation to `insufficient_evidence` if any compared candidate currency is not HKD or any required close is absent/unavailable/conflicting. This prevents cross-currency economics and selection from a partially evaluable set of levels.
6. Call W1B economics for each required baseline/challenger using `holding_start_date=trading_date`, frozen `net_premium`, `net_cash_basis`, `strike`, `multiplier`, the exact close, and unchanged account fee plan. Any not-evaluable result makes the entire research evaluation insufficient with its reason/detail.
7. For each variant, call W1B statistics with fixed policy `{required_days:40, confidence_level:0.95, worst_fraction:0.20, require_concentration_non_increase:true}`.
8. If any variant is `insufficient_evidence`, overall selection is `insufficient_evidence` and no leader is emitted. Otherwise collect variants with decision `pass`; none means `no_research_winner`.
9. Sort passing variants by negative mean, negative lower bound, negative worst-tail mean, then `variant_id` ascending; emit only the first as `research_leader`.

No intermediate value is rounded. Input ordering of close receipts cannot change output. Variant output order follows the authorized spec.

## 7. Output contract and storage decision

Return exact schema `sell_put_top1_research_evaluation.v1` with:

```text
schema_version, experiment_id, research_spec_sha256,
dataset_ref, dataset_sha256, dataset_content_sha256,
required_days, effective_days,
research_fill_assumption, research_is_counterfactual,
contract_terms_revalidated,
selection, leader_variant_id, reason_codes, reason_details,
variant_results, missing_receipts
```

Each `variant_result` contains only variant/profile identity, the existing statistics decision/reasons and aggregate scalars, `top1_change_count`, and the 40 compact daily deltas/effective-point counts. It omits duplicated candidate rows, point-level economics, raw provider payloads, and statistics `point_results`. The W4 projections and future close/fee artifacts remain the audit facts. This keeps the later research receipt compact without losing recomputability.

Each `variant_result` has these exact keys:

```text
variant_id, ranking_profile, decision, reason_codes,
required_days, effective_days,
mean_daily_delta, sample_std, standard_error, t_critical,
one_sided_lower_bound, worst_k, worst_tail_mean,
serial_correlation_unadjusted, top1_change_count, daily_deltas
```

Each daily item keeps the existing statistics exact keys `trading_date`, `effective_point_count`, and `daily_delta`.

`missing_receipts` items have exact keys `stock_owner`, `expiration`, `reason_code`, and `reason_detail`, sorted by that tuple. Structurally valid unused receipt facts never appear. Top-level `effective_days` is `null` when evaluation stops before statistics; otherwise it is the minimum `effective_days` across all authorized variants. Top-level reason codes and reason details are first-seen de-duplicated in authorized variant order; pre-statistics reason/missing sets use lexical tuple order. These rules, authorized variant order, and sorted missing receipts make input receipt ordering irrelevant.

Selection fields are exact: `research_leader` copies the selected variant's three passing reason codes and has no reason details; `no_research_winner` uses only `reason_codes=["no_research_winner"]`; `insufficient_evidence` aggregates only the blocking evidence reasons/details and always has `leader_variant_id=null`.

The payload is explicitly unsealed. A later, separately reviewed `run_research()` will own reading exact bytes, provider receipts, revision publication, and M3 terminal sealing.

## 8. M4 and M3 seams

- M4 seam test freezes a real W4 manifest, materializes its referenced projections, and proves the evaluator accepts the exact W4 refs/hashes after source-run deletion. The test performs local fixture I/O only; production evaluator remains pure.
- M3 seam test uses a synthetic completed research generation and the evaluator's exact `leader_variant_id` as `system_leader`/challenger. It proves a different challenger is rejected and the accepted lock remains `validation_authorization_status=unconfirmed`; no validation starts without separate human authorization.

The M3 seam does not claim the evaluation payload itself has been sealed. It supplies synthetic receipt ref/hash solely to exercise the already implemented M3 boundary.

## 9. Error and conclusion rules

- Malformed public input, schema drift, unsafe identity, ref/hash mismatch, incomplete projection materialization, or baseline parity failure raises `ResearchEvaluationError` with a stable reason code and no result. Its reason-code set is exactly `research_input_invalid | experiment_spec_invalid | research_corpus_conflict | ranking_projection_incomplete | baseline_rank_parity_mismatch`; W1A errors retain the latter two codes and other malformed shapes use `research_input_invalid`.
- Missing/unavailable exact close, incomplete canonical fee evidence, no-evidence days, statistics backend failure, or an evaluable statistical gate produces a normal deterministic evaluation result.
- A compared non-HKD candidate is a normal insufficient result with `ranking_projection_incomplete / candidate_currency_mismatch`; it never reaches W1B economics.
- A duplicate required close is a normal insufficient result with `required_outcome_missing / expiry_close_receipt_conflict`; unused duplicate keys do not affect the result.
- Any evidence-insufficient variant blocks leader selection for the whole research window.
- A keep-baseline variant does not block another fully evaluable passing variant.
- `research_leader` is model-selection evidence only, never `candidate_for_adoption` and never a production configuration change.

## 10. Implementation slice S1 — pure evaluator and seams

Objective: implement and verify the complete narrowed work unit in one reviewable slice.

Allowed production file:

- `src/application/strategy_lab/top1/research.py`.

Allowed supporting changes:

- `tests/test_strategy_lab_top1_research.py`;
- W5 architecture assertion and dependency graph output;
- this work unit's Gateflow/review artifacts.

Required tests:

- exact 40-day, two-points-on-one-day hand calculation including terminal fee and daily mean;
- all authorized levels rerank the same projection and one unique leader is selected;
- multiple passing variants use every deterministic tie-break key, including lexical variant ID last;
- zero/same Top1 produces `no_research_winner` without needing close/fee facts;
- one missing/unavailable/duplicate exact close blocks the whole selection with no fallback;
- incomplete fee facts and concentration increase fail closed through the existing owners;
- a compared USD candidate in an otherwise valid HK projection is insufficient, never calls economics, and never emits a leader;
- fewer than 40 effective days yields insufficient evidence;
- W4 manifest/projection hash or baseline parity tampering is rejected;
- close-receipt order does not change output;
- reason aggregation, overall effective days, missing-receipt tuple ordering, and variant-result exact keys match §7;
- W4 source deletion seam and M3 exact-leader/human-authorization seam pass;
- evaluator has no filesystem, store, provider, validation, service, CLI, or LLM import.

Validation commands:

```bash
./.venv/bin/python -m pytest -q tests/test_strategy_lab_top1_research.py tests/test_strategy_lab_top1_w1b.py tests/test_strategy_lab_top1_corpus.py tests/test_strategy_lab_top1_store.py tests/test_strategy_lab_top1_architecture.py -p no:cacheprovider
./.venv/bin/python -m ruff check src/application/strategy_lab/top1/research.py tests/test_strategy_lab_top1_research.py tests/test_strategy_lab_top1_architecture.py
./.venv/bin/python -m basedpyright src/application/strategy_lab/top1/research.py tests/test_strategy_lab_top1_research.py
./.venv/bin/python scripts/generate_dependency_graph.py --check
git diff --check
```

Completion signal: all required assertions pass, Kimi code/aggregate/PR reviews have no unresolved accepted finding, and the draft PR truthfully states that the provider runner and real W5 remain deferred.

Stop condition: any evidence that the evaluator requires a new provider/storage schema, changes Candidate Engine behavior, or cannot prove the M4/M3 seams without production I/O returns the work unit to goal confirmation rather than expanding scope.

## 11. Documentation, risks, and completion report

No product or operator documentation changes because there is no public command or runtime capability. Gateflow artifacts document the internal contract and the explicit partial-W5 boundary.

Classified residual risks:

- Real close/quota/calendar/fee-plan acquisition and exact artifact sealing: assigned to the remaining W5 runner work after W0R becomes green.
- Provider retries, request dedupe, endpoint limits, and capacity: assigned to the remaining W5 runner/W7 readiness work.
- Real 40-day strategy conclusion: assigned to an explicitly authorized pilot after release/install readiness.
- Generic hard-risk failure generation: owned by W1B statistics; valid W4 accepted-only inputs make W5's status `passed` by construction.

Completion report must state changed code, validation evidence, review finding status, draft PR URL, and the above deferred owners. It must not call the original W5 complete and must state that no release/deploy/provider/live experiment occurred.
