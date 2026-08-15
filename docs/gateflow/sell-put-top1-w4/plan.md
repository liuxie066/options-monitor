# Gateflow Plan — Sell Put Top1 W4

- Gate: `plan`
- Work unit: `sell-put-top1-w4`
- Branch: `feat/sell-put-top1-w4`
- Base: `origin/main@baa681628f0b62a43f48edd51e2e5fb4f4fafa8e`
- Goal contract: `docs/gateflow/sell-put-top1-w4/goal-confirmation.md`
- Design sources:
  - `docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md`
  - `docs/plans/sell-put-top1-modular-technical-implementation-plan-20260814.md`
  - `docs/plans/sell-put-top1-modular-implementation-control-20260814.md`
- Artifact path: `docs/gateflow/sell-put-top1-w4/plan.md`
- Current gate: plan review pending

## 1. Goal, motivation, and completion signal

W4 adds the smallest durable Corpus seam between official M2 recommendation points and later W5 research. It freezes the denominator before the day begins, copies only W1A's accepted-candidate ranking projection, reports coverage without looking at results, and creates a deterministic reference-only dataset for exactly one caller-certified mature 40-day window.

Completion requires all assertions in §13, no unresolved accepted review finding, no dependency reversal, and a Kimi aggregate DeepReview pass. Synthetic success proves the Corpus contract only; it does not claim a better strategy, a runnable real research window, or a completed 40+20 experiment.

## 2. Non-goals and fixed boundary

- No W5 research economics, historical close/fee read, statistics, arm comparison, leader, or research terminal.
- No W6 hidden validation, fill observation, outcome job, or 20-day result.
- No W7 CLI, timer, service renderer/installation, profile, scheduler invocation, or automatic experiment transition.
- No W8 LLM, Prompt, Agent tool, GitHub issue, or hypothesis generation.
- No Candidate Engine/filter/ranking changes, provider/OpenD access, production config, notification, ledger, release, deploy, or live pilot.
- No generic Corpus framework, repository protocol, ORM, event bus, queue, workflow engine, capability registry, or future schema tables.
- No rejected candidates, raw chain, broker response, quote series, source snapshot copy, projection BLOB, or expected-point array in SQLite.

## 3. First-principles judgment and existing owners

The work unit is necessary because source runs are retention-bound while a research window needs exact historical point denominators and rerankable accepted facts. Storing only observed points would silently shrink the denominator; retaining whole source runs would violate the storage objective.

Reuse these owners:

- `scan_scheduler.py`: sole calculation of official report/candidate-check targets.
- `recommendation_point.py`: point identity, canonical point validation, and source loading.
- `opening_candidate_snapshot.py`: exact current-contract snapshot loading/validation.
- `ranking.py`: accepted-only projection, provenance validation, baseline parity, and source-independent reranking.
- `lifecycle.effective_feature_status()`: maintainer plus user feature gate.
- `terminal_projection.publish_exact_text()`: safe private write-once/adopt-exact bytes.
- `ExperimentStore`: one SQLite authority and transaction boundary.

W4 does not create `top1/readiness.py`: only Corpus status exists now, so `read_corpus_status()` stays beside the Corpus behavior until W7 has actual cross-capability readiness to own.

## 4. Affected files and dependency direction

### Production

- New `src/application/strategy_lab/top1/corpus.py`.
- Modify `src/application/scan_scheduler.py` with one public pure target-for-date wrapper around existing private calculation.
- Modify `src/infrastructure/strategy_lab/experiment_store.py` for schema v2 and narrow Corpus index commands/reads.

### Tests and generated docs

- New `tests/test_strategy_lab_top1_corpus.py`.
- Modify `tests/test_strategy_lab_top1_store.py` for v1 -> v2 migration and schema fail-closed coverage.
- Modify `tests/test_strategy_lab_top1_architecture.py` for W4 dependency guards.
- Regenerate `docs/DEPENDENCY_GRAPH.md` and `docs/dependency_graph.mmd` only for actual new imports.
- Add Gateflow/review artifacts under `docs/gateflow/sell-put-top1-w4/` and `docs/reviews/`.

Dependency rules:

```text
scan_scheduler -> MUST NOT import Strategy Lab
experiment_store -> stdlib/sqlite3 + private_storage only
corpus -> scheduler + M1A/M2/M3 public owners + private artifact primitives
Candidate Engine / producer tick -> MUST NOT import corpus or store
```

## 5. Public application contract

`corpus.py` exposes:

```python
seal_day_expectation(
    store, artifact_root, *, market, account, schedule, trading_date,
    market_calendar_version, market_calendar_sha256, sealed_at_utc,
    environ=None,
) -> dict

capture_recommendation_point(
    store, source_root, artifact_root, *, point_ref, trading_date,
    captured_at_utc,
    environ=None,
) -> dict

read_corpus_status(store, *, market, account) -> dict

freeze_research_dataset(
    store, artifact_root, *, window_facts, required_days=40,
    environ=None,
) -> dict
```

`scheduled_scan_targets_for_date(schedule_cfg, trading_date)` is added to `scan_scheduler.py`. It uses the same timezone, run-window, break, run-point, candidate-check, and gate logic as `decide()`. Disabled schedules and non-trading weekdays return no targets. No second schedule parser is created.

`required_days` is retained only to match the approved design signature; the public function rejects every value other than `40`, matching W1B's fixed ExperimentSpec contract.

`CorpusError.reason_code` is limited to `corpus_input_invalid | schema_unsupported | corpus_artifact_invalid | corpus_artifact_conflict`. Expected feature/coverage/evidence states use the exact result contract in §10 rather than exceptions.

## 6. Immutable artifact contracts

### `corpus_day_expectation.v1`

Exact fields:

- schema, first-release `HK`, lowercase account, canonical trading date; US remains a separately confirmed capability expansion under the parent design;
- market-calendar version/hash and canonical schedule-config hash;
- original `sealed_at_utc`, first target UTC, and `sealed_before_first_target`;
- all canonical UTC `scheduled_scan_targets_market` in order;
- aligned precomputed `expected_recommendation_point_ids`;
- semantic `content_sha256` over every preceding field.

Path:

```text
strategy_lab/top1/corpus/<market-lower>/<account>/days/<trading-date>.expectation.json
```

The exact pretty-canonical bytes have a separate file SHA-256. A retry with the same denominator adopts the first artifact and its original seal time; a changed target list, schedule hash, calendar binding, or bytes marks the day conflict and never overwrites.

### Captured ranking projection

The artifact is the unchanged `sell_put_ranking_projection.v1` returned by W1A. It contains accepted `U_rank` facts only.

Path:

```text
strategy_lab/top1/corpus/<market-lower>/<account>/points/<recommendation-point-id>.json
```

The point ID is the natural content address. Same canonical projection bytes adopt exactly. Different point/projection facts mark `research_corpus_conflict`; the old artifact and original index facts remain unchanged.

### `sealed_historical_dataset.v1`

The dataset contains no candidate rows. It binds:

- account/market, cutoff, required day count, validated window-facts hash, calendar version/ref/hash, calendar-date-list hash, and maturity evidence ref/hash;
- fixed selector `official_scheduled_sell_put.v1` and projection schema version;
- exactly 40 ordered trading dates;
- for each date, the expectation ref/content/file hash and ordered point projection refs/content/file hashes;
- semantic dataset content hash.

Path is content-addressed by dataset hash:

```text
strategy_lab/top1/corpus/<market-lower>/<account>/datasets/<dataset-content-sha256>.json
```

No SQLite dataset table is added. W5 later stores only this returned ref/hash in an authorized ExperimentSpec.

### `sell_put_top1_research_window_facts.v1`

The caller supplies one exact-key mapping rather than unrelated scalar claims:

- schema, market, account, canonical `cutoff_at_utc` and `cutoff_trading_date`;
- market-calendar version plus safe evidence ref/hash;
- the complete strictly increasing trading-date sequence through the cutoff and `trading_calendar_dates_sha256 = canonical_sha256(sequence)`;
- nullable `latest_mature_trading_date` plus safe maturity-evidence ref/hash;
- fixed selector `official_scheduled_sell_put.v1`;
- `content_sha256` recomputed over every preceding field.

This object binds the selection inputs to their evidence without making W4 a provider validator. The frozen dataset stores its content hash, selected 40 dates, calendar-date-list hash, and both external evidence refs/hashes; it does not duplicate the full caller sequence.

## 7. Feature, source, and capture semantics

Every persistent command checks `effective_feature_status()` before artifact or SQLite writes. Feature off returns `feature_disabled`; source point reading needed to discover its market is allowed, but no Corpus state changes.

`capture_recommendation_point()` accepts a canonical trading date and only the canonical M2 ref:

```text
output_runs/<run-id>/accounts/<account>/state/recommendation_point.sell_put.json
```

Before any projection artifact or point-index write, capture loads the already indexed expectation for that exact market/account/date, validates its immutable artifact, and proves that the point ID is an expected member. Missing, late, empty, or conflicting expectation and wrong-date/unexpected point return `not_evaluable` without a point row or projection artifact.

It then:

1. loads and validates canonical point bytes;
2. rejects ref/body identity disagreement;
3. validates membership in the day's expectation as described above;
4. records expected `partial_data|data_unavailable` as `not_evaluable / official_decision_incomplete` without building a projection;
5. for clean `candidates_found|no_candidate`, loads the bound current-contract opening snapshot, verifies its hash and the point's accepted-ID sequence, and calls `build_ranking_projection()`;
6. publishes/adopts exact projection bytes first, then binds the immutable ref/hashes in SQLite.

Artifact-first is deliberately used instead of another outbox: a crash can leave only an inert content-addressed orphan, and the same command deterministically adopts it on retry. SQLite never points at unpublished bytes. Conflict never overwrites either side.

## 8. SQLite schema v2

`SCHEMA_VERSION` becomes `2`.

Migration accepts fresh/empty, synthetic v0, valid v1, and valid v2 stores. A valid v1 store is reported as `migration_required`, then upgraded transactionally by adding only the two W4 tables and updating metadata. Unknown/corrupt/malformed layouts fail closed without rebuild.

### `strategy_lab_corpus_days`

Primary key `(market, account, trading_date)` with:

- expectation ref/content/file hashes;
- calendar version/hash, schedule hash, expected count;
- first target, original seal time, sealed-before-first-target boolean;
- static completeness reason (`NULL`, late seal, empty denominator, or expectation conflict);
- clean/conflict marker.

The target and point-ID arrays remain only in the immutable expectation artifact.

### `strategy_lab_corpus_points`

Primary key `(market, account, recommendation_point_id)` with:

- trading date, source run/point ref/content hash, snapshot ref/hash;
- projection schema/ref/content/file hashes when captured;
- original capture time;
- `captured|not_evaluable`, stable reason, and clean/conflict marker.

CHECK constraints enforce the nullable projection fields as one group. Store commands preserve original facts, return exact retry as idempotent, and only move the conflict marker from clean to conflict on disagreement.

No foreign key connects Corpus to experiments because the Corpus is account-scoped reusable evidence and can exist with zero experiments.

## 9. Coverage and fixed-window selection

The caller supplies one validated `sell_put_top1_research_window_facts.v1`. W4 recomputes its date-list and full content hashes, validates identity/cutoff/order/ref fields, and otherwise does not query or infer provider truth.

Selection is deterministic:

1. require `required_days == 40`;
2. find `latest_mature_trading_date` in the bound full calendar;
3. take exactly the preceding 40 entries ending there;
4. if fewer exist, return `research_corpus_warming`;
5. inspect only that one fixed window;
6. any missing/late/conflicting expectation, calendar mismatch, empty denominator, absent/non-evaluable/conflicting point, missing/tampered projection, or baseline parity failure returns `research_corpus_conflict` when conflict exists, otherwise `research_dataset_coverage_missing`;
7. never search an older window after step 5;
8. publish/adopt the reference-only dataset only after every check passes.

`latest_mature_trading_date=None` is a lawful warming state. Tests construct the real 40-date contract; there is no smaller public or test-only production mode.

## 10. Compact status

All command results use `sell_put_top1_corpus_command_result.v1` and exact keys: `schema_version`, `operation`, `status`, `reason_code`, `market`, `account`, `trading_date`, `recommendation_point_id`, `artifact_ref`, `artifact_sha256`, `artifact_content_sha256`, and `expected_point_count`. Non-applicable values are `null`; keys never vary by branch. `artifact_sha256` is the exact canonical file-byte hash used by later refs.

- seal status: `published | idempotent | not_evaluable | conflict`;
- capture status: `published | idempotent | not_evaluable | conflict`;
- stable non-conflict reasons: `feature_disabled | corpus_day_expectation_late | corpus_day_expectation_empty | corpus_day_expectation_missing | corpus_day_not_evaluable | unexpected_recommendation_point | official_decision_incomplete | opening_snapshot_missing | opening_snapshot_conflict | ranking_projection_incomplete`;
- any evidence disagreement uses `status=conflict, reason_code=research_corpus_conflict`.

Freeze uses `sell_put_top1_dataset_freeze_result.v1` with literal exact keys: `schema_version`, `status`, `reason_code`, `market`, `account`, `window_facts_content_sha256`, `selected_trading_dates`, `dataset_ref`, `dataset_sha256`, and `dataset_content_sha256`. Status is `ready | blocked`; the three dataset fields are null unless ready. `dataset_sha256` is the exact file-byte hash consumed by W1B `research_source.dataset_sha256`. Blockers are exactly `research_corpus_warming | research_dataset_coverage_missing | research_corpus_conflict`.

`read_corpus_status()` returns literal exact `sell_put_top1_corpus_status.v1` keys: `schema_version`, `market`, `account`, `days_total`, `days_on_time`, `days_not_evaluable`, `days_conflicting`, `expected_points_total`, `points_captured`, `points_not_evaluable`, `points_conflicting`, `points_missing`, `earliest_trading_date`, `latest_trading_date`, and `ranking_projection_schema_version`. It reads SQLite only and does not claim mature-window readiness because window facts are absent.

## 11. State transitions and conflict invariants

```text
day absent -> sealed(on-time | late | empty)
day same denominator -> idempotent
day different denominator/artifact -> conflict (terminal, old facts retained)

point absent -> captured | not_evaluable
point same facts -> idempotent
point different source/projection/artifact -> conflict (terminal, old facts retained)
```

Neither conflict state can return to clean in W4. Missing expected points are never synthesized from current market data.

## 12. Implementation slices

### S1 — Seal the denominator and capture official point evidence

- Allowed production files: `scan_scheduler.py`, `experiment_store.py`, new `corpus.py`.
- Allowed tests: new Corpus tests, store migration tests, architecture guard, generated dependency graph.
- Implement schema v2, public target-for-date wrapper, exact expectation publication/indexing, feature gate, point-ref/date/expectation membership validation, clean/not-evaluable capture, exact result envelopes, and idempotent/conflict semantics.
- Non-goals: status aggregation, calendar/maturity window selection, dataset artifact.
- Completion signal: fresh/v0/v1/v2 migration; feature-off no-write; on-time/late/drift expectation; wrong-date/missing-expectation/unexpected point; clean/no-candidate/partial point; same/different hash; and M2 point -> M4 projection seam all pass.

### S2 — Prove coverage and freeze exactly one mature 40-day window

- Allowed production file: `corpus.py`; store receives only a missing compact read query if S1 evidence proves it necessary.
- Add strict artifact readers, exact window-facts validation/hash binding, status aggregation, fixed 40-date selection, gap/conflict priority, projection baseline rerank validation, and deterministic reference-only dataset publication.
- Non-goals: outcome maturity calculation, research evaluation, provider reads, experiment binding.
- Completion signal: complete synthetic 40-day freeze, rejected non-40 request, fewer-day warming, latest-window gap without older fallback, any window-facts field/hash tampering, source deletion rerank, exact compact status, and S1 regression pass.

Two slices are justified because persistence/capture and read-only window freezing are independently reviewable behavior boundaries. A third slice would add gate cost without isolating another meaningful risk.

## 13. Tests and expected assertions

Focused commands:

```text
python -m pytest -q tests/test_strategy_lab_top1_corpus.py tests/test_strategy_lab_top1_store.py
python -m pytest -q tests/test_recommendation_point.py tests/test_strategy_lab_top1.py tests/test_strategy_lab_top1_architecture.py
python -m pytest -q tests/test_scan_scheduler_notify_semantics.py tests/test_scan_scheduler_scan_per_account.py
ruff check src/application/scan_scheduler.py src/application/strategy_lab/top1/corpus.py src/infrastructure/strategy_lab/experiment_store.py tests/test_strategy_lab_top1_corpus.py tests/test_strategy_lab_top1_store.py tests/test_strategy_lab_top1_architecture.py
python scripts/generate_dependency_graph.py --check
git diff --check
```

Before aggregate closeout, run the complete local pytest suite because the schema version and scheduler helper are shared boundaries.

Required behavioral assertions:

- feature absent/user-off/maintainer-off creates no day/point artifact or row;
- expectation is complete, ordered, before-first-target aware, byte-idempotent, and drift-conflicting;
- point ref/body/date/expectation membership/snapshot/accepted order/projection hashes are all verified;
- source `output_runs` deletion does not affect projection validation or reranking;
- no rejected/raw/source rows appear in Corpus artifacts or DB;
- v1 data and lifecycle behavior survive v2 migration unchanged;
- selection uses exact calendar adjacency and never chooses an older clean window after a latest-window gap;
- window-facts calendar list, cutoff, mature date, evidence refs/hashes, and full content hash are one tamper-evident contract;
- dataset contains 40 days and references only exact immutable artifacts;
- all public result envelopes use exact keys/status/reason values and non-40 freeze is rejected;
- production tick and Candidate Engine remain independent from Corpus/store.

## 14. Documentation decision

The approved product/technical plans already describe W4 correctly and are not rewritten. Only dependency graphs and Gateflow/review/closeout evidence change. No public CLI documentation changes because W4 introduces no CLI.

## 15. Risks and residual ownership

- Real calendar/maturity/provider evidence is intentionally absent in W4 and remains W0R/W5 ownership; synthetic hashes do not make runtime ready.
- W7 must call expectation sealing before the first target and consume points within source retention; W4 supplies behavior but installs no timer.
- A content-addressed orphan may remain after a crash before SQLite binding. It is inert and adoptable; cleanup is not added without measured growth.
- Projection schema upgrades require new future Corpus evidence; W4 never backfills missing fields into old points.

All are classified as later approved work units, not unowned W4 findings.

## 16. Completion report format

- changed production/test/docs files;
- focused, architecture, lint, dependency, and full-suite results;
- plan/code/Kimi/PR finding disposition;
- schema migration and stable W4 public contracts exposed to W5/W6;
- remaining W0R/W5/W7 risks and owners;
- draft PR URL and next entry point (`merge W4`, then W5 goal confirmation).

## Next gate

`plan review`
