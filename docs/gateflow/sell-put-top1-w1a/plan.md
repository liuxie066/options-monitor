# Gateflow Plan — Sell Put Top1 W1A

- Gate: `plan`
- Work unit: `sell-put-top1-w1a`
- Branch: `feat/sell-put-top1-w1a`
- Base: `main@c1d759ae10352d2a5664739e2053bb396e698919`
- Artifact path: `docs/gateflow/sell-put-top1-w1a/plan.md`
- Current gate: `plan`
- Next entry point: `plan review`

## Goal, motivation, and success signal

Deliver the deterministic ranking/projection foundation required by every later Sell Put Top1 experiment module. Candidate Engine remains the only ranking owner; Strategy Lab receives only a strict projection and calls the public Candidate Engine API.

The work unit passes when default production ranking is byte-for-byte/order-for-order compatible, all three approved profiles have exact fixtures, a complete sealed snapshot can be projected and reranked after the source snapshot is gone, malformed projections fail closed, and no forbidden dependency or production side effect is introduced.

## Non-goals and scope boundary

- No filtering or accepted-set changes.
- No ExperimentSpec lifecycle, behavior-binding orchestration, economics, statistics, SciPy, SQLite, persistence, point publisher, corpus, provider, CLI, service, Agent tool, Prompt, or LLM.
- No changes to current fee remediation files or unrelated dirty files.
- No config, notification, broker, ledger, release, deployment, or service mutation.
- No abstraction for future ranking variables; only the three confirmed profiles exist.

## Design alignment and gate correction

The product contract requires the same producer-owned `U_rank`, a versioned `sell_put_ranking_profile.v1`, and `sell_put_ranking_projection.v1`. W1A implements only those contracts.

The current W0 evidence remains `no-go` for real provider-dependent research/validation. Plan documents will be corrected so that:

- `W0A` is the source-build/static contract gate and permits W1A–W4 source implementation;
- `W0R` is the provider/account capability gate and must pass before provider-dependent research/validation or any real pilot;
- missing historical corpus remains `research_corpus_warming` and never authorizes synthetic production evidence.

This removes the circular requirement that W0 prove APIs and corpus that later modules must first create, without weakening runtime safety.

## First-principles judgment and direct code evidence

- `rank_candidate_rows()` already performs grouping, return-band ranking, and tie breaking; extending its existing cross-symbol branch is smaller and safer than adding a Strategy Lab sorter.
- Opening snapshots already seal normalized accepted facts, candidate IDs, producer ranks, and hashes. W1A projects those existing facts rather than recalculating policy or metrics.
- The default Candidate Engine API is already consumed by opening snapshots, reports, underwriting, and tests; therefore the new argument must be keyword-only with the existing default.

## Public contracts

### Candidate Engine

Add:

```python
SELL_PUT_RANKING_CONTRACT_VERSION = "sell_put_ranking_profile.v1"
SELL_PUT_RANKING_PROFILES = frozenset({
    "without_concentration",
    "current_tie_break",
    "concentration_first",
})

rank_candidate_rows(
    rows,
    *,
    mode,
    sell_put_ranking_profile="current_tie_break",
)
```

Rules:

- `current_tie_break`: existing order exactly.
- `without_concentration`: retain return bands; remove concentration only from the cross-symbol tie key.
- `concentration_first`: rank cross-symbol representatives by canonical concentration ascending, unknown last; only representatives with exactly equal canonical concentration use existing return bands and the remaining cross-symbol tie keys.
- Within-symbol ranking is unchanged for all profiles.
- Covered Call accepts only the default profile; a non-default Sell Put profile with `mode="call"` raises `ValueError`.
- Omitted profile remains production compatible.

### Ranking projection

Add pure functions in `src/application/strategy_lab/top1/ranking.py`:

```python
build_ranking_projection(
    opening_snapshot,
    *,
    point_binding,
) -> dict

validate_ranking_projection(payload) -> dict

rerank_recommendation_point(
    projection,
    *,
    ranking_profile="current_tie_break",
) -> dict
```

`point_binding` 是 W2 后续直接生成的最小 producer binding 投影，W1A 只做纯校验，不发布 point。它的精确 required keys 为：

- `recommendation_point_id`：64 位小写 SHA-256 hex；
- `market`、`account`、`run_id`；
- `opening_snapshot_ref`：runtime root 下的 safe relative POSIX path，禁止绝对路径、`.`、`..` 和空 segment；
- `opening_snapshot_sha256`：64 位小写 SHA-256 hex；
- `decision_at_utc`：UTC ISO-8601 `Z`；
- `source_commit_sha`：40 位小写 Git SHA hex。

`market/account/run_id/opening_snapshot_sha256` 必须与 opening snapshot 精确一致。W1A 不声称这份结构本身证明 producer 发布成功；W2 仍是 `recommendation_point.v1` 的唯一发布和 write-once owner。

Projection top-level required fields:

- `schema_version=sell_put_ranking_projection.v1`
- `recommendation_point_id`, `market`, lowercase `account`, `run_id`
- `opening_snapshot_ref`, `opening_snapshot_sha256`, `decision_at_utc`
- `source_commit_sha`, `account_config_sha256`, `strategy_policy_sha256`
- `sell_put_ranking_contract_version`
- ordered `producer_accepted_candidate_ids`
- `candidates`
- `artifact_provenance`：复用 `research_artifact_provenance.v1`，`artifact_kind=sell_put_ranking_projection`，`source_generation` 绑定 opening snapshot content hash/ref

Each candidate must contain every key below; nullable ranking facts remain explicit and are never defaulted or read from aliases:

- Identity/order: `candidate_id`, `symbol`, `contract_symbol`, `producer_rank`
- Ranking: `period_net_return_on_cash_basis`, `net_assignment_discount_pct`, `spread_ratio`, `open_interest`, `net_income_cny`, `net_income`, `symbol_concentration_after`
- Economic/result binding: `sell_limit`, `net_premium`, `net_cash_basis`, `expiration`, `strike`, `multiplier`, `currency`, `stock_owner`, `fee_schedule_version`, `fee_basis`, `fee_schedule_url`

Builder invariants:

- Validate the current opening snapshot contract in memory without filesystem access.
- Validate the exact `point_binding` key set, safe relative snapshot ref, hash lengths, UTC time, and all available point/snapshot cross-field bindings.
- Use only accepted Sell Put decisions and the producer Sell Put rank.
- Accepted IDs must match the producer-ranked Sell Put IDs exactly.
- Copy only canonical normalized-input keys; no aliases, fallbacks, recomputation, rejected rows, raw quotes, or extra candidate fields.
- Reuse `attach_artifact_provenance()` to attach the single repository-owned content hash; do not add a second top-level hash.

Validator invariants:

- Exact schema version and contract version.
- Exact top-level, `point_binding`, candidate, and provenance key sets; no silent extra fields.
- Non-empty identity/provenance strings, lowercase account, uppercase `HK|US`, valid UTC timestamp, exact 64/40-character lowercase hashes, safe relative ref, positive rank/price/basis/multiplier fields, finite numeric-or-null ranking facts.
- Candidate IDs are unique; ranks are contiguous from 1; ordered IDs equal candidate order.
- `validate_artifact_provenance()` proves artifact kind, source generation, and the one canonical `artifact_provenance.content_sha256`.
- Failure raises `Top1RankingError` with `reason_code="ranking_projection_incomplete"`.

Rerank invariants:

- Operate only on the validated projected candidates.
- `current_tie_break` reranks with Candidate Engine and requires exact ID parity with producer order; mismatch raises `baseline_rank_parity_mismatch`.
- Other profiles may change only order, never IDs or candidate facts.
- Return `sell_put_recommendation_ranking_result.v1` with profile, `artifact_provenance.content_sha256`, ordered IDs, nullable Top1 ID, and parity status.
- Empty accepted set is valid and returns an empty order and null Top1.

## Affected files and allowed changes

### Production

- `domain/domain/engine/candidate_engine.py`: constants, profile validation, two narrow cross-symbol helpers, keyword-only parameter.
- `domain/domain/engine/__init__.py`: export the new stable constants.
- `src/application/strategy_lab/top1/__init__.py`: empty package marker only.
- `src/application/strategy_lab/top1/ranking.py`: projection and reranking pure functions.

W1A may import only the existing pure provenance/hash functions from `src.application.shadow_replay.common`; it must not call that module's file readers/writers or create a second renderer/hash helper.

### Tests

- `tests/test_candidate_engine_contract.py`: exact three-profile fixtures and Covered Call rejection.
- `tests/test_candidate_engine_parity.py`: omitted/default profile compatibility and production-call parity.
- `tests/test_strategy_lab_top1.py`: projection golden, source-independent rerank, empty set, strict missing/extra/type/hash/parity failures.
- `tests/test_strategy_lab_top1_architecture.py`: import-boundary guard.

### Plan/Gateflow artifacts

- Narrow gate/split corrections in the three Sell Put Top1 plan documents.
- `docs/gateflow/sell-put-top1-w1a/` artifacts.
- Timestamped PlanReview/DeepReview artifacts required by Gateflow.

No other file is allowed without a new Gateflow decision.

## Data flow and error handling

```text
producer point_binding + opening_candidate_snapshot.v1
  -> validate current snapshot
  -> cross-check point market/account/run/snapshot hash
  -> copy accepted Sell Put canonical fields
  -> attach/validate research_artifact_provenance.v1
  -> Candidate Engine rank_candidate_rows(profile)
  -> verify same IDs / default parity
  -> sell_put_recommendation_ranking_result.v1
```

All failures are local exceptions with stable reason codes. There are no writes, retries, recovery states, clocks, network reads, or partial commits in W1A.

## Implementation slice

### W1A-S1 — ranking profile and replayable projection

- Objective: implement all Candidate Engine and projection contracts above.
- Prerequisites: accepted goal confirmation and PlanReview pass.
- Allowed files: exactly the production/test files listed above.
- Expected outcome: pure deterministic module with no change to omitted-profile production behavior.
- Stop conditions: second ranking owner, need for policy/metric recomputation, ambiguous source field, default parity regression, forbidden dependency, or required modification outside allowed files.
- Completion signal: focused tests and guards pass; implementation artifact exists; Kimi DeepReview loop has no unresolved accepted finding.

## Validation

```bash
./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_candidate_engine_contract.py \
  tests/test_candidate_engine_parity.py \
  tests/test_opening_candidate_snapshot.py \
  tests/test_strategy_lab_top1.py \
  tests/test_strategy_lab_top1_architecture.py

./.venv/bin/ruff check \
  domain/domain/engine/candidate_engine.py \
  domain/domain/engine/__init__.py \
  src/application/strategy_lab/top1 \
  tests/test_candidate_engine_contract.py \
  tests/test_candidate_engine_parity.py \
  tests/test_strategy_lab_top1.py \
  tests/test_strategy_lab_top1_architecture.py

./.venv/bin/python scripts/generate_dependency_graph.py --check
./.venv/bin/python -m basedpyright \
  domain/domain/engine/candidate_engine.py \
  src/application/strategy_lab/top1
git diff --check
```

Expected assertions:

- Current callers and Covered Call results are unchanged when the profile is omitted.
- All profile fixtures produce exact documented orders, including equal and missing concentration groups.
- Projection reranks after deleting the source object/reference fixture.
- Every required point/candidate/provenance key removal, extra key, unsafe/absolute ref, point/snapshot mismatch, invalid numeric/hash, duplicate ID/rank, and producer/default parity mismatch fails closed.
- Architecture guard proves Candidate Engine does not import Strategy Lab and W1A does not import filesystem/provider/storage/interface/service/LLM modules.

If BasedPyright is unavailable in the repository environment, do not install a new dependency in this work unit; record the missing tool as validation evidence and rely on import/compile tests plus Ruff until the project toolchain is separately repaired.

## Documentation decision

Update the product/technical/control plans only where needed to distinguish build readiness from provider/pilot readiness and split W1A from later economics/statistics. Do not rewrite product behavior or add future capability sections.

## Risks and classification

- Float exactness in concentration groups: fixed in current slice by validating finite canonical numeric values and using exact equality as the approved contract states.
- Historical snapshots that lack v1 projection fields: covered by later corpus readiness; W1A fails closed and never backfills aliases.
- Account-specific terminal fees and provider receipts: covered by `W0R`/later economics work units.
- Existing fee changes in the dirty worktree: assigned to a separate work unit and excluded from staging.
- No real opening snapshots currently retained: synthetic complete snapshot fixtures prove code behavior; real corpus/pilot evidence remains blocked by later readiness gates.

## Completion report format

- Changed files and accepted commit hashes.
- Focused tests, architecture guard, Ruff, dependency graph, type-check availability, and `git diff --check` results.
- Kimi DeepReview artifact and final finding statuses.
- Residual risks with later owner.
- Draft PR URL and next module entry point after Gateflow final closeout.

## Why this is not overdesigned

W1A adds one optional argument at the existing ranking owner and one pure projection module. It reuses opening snapshot facts and canonical hashing, adds no dependency, storage, service, framework, registry, interface hierarchy, or future parameter abstraction. Everything added is exercised by the first confirmed ranking experiment.
