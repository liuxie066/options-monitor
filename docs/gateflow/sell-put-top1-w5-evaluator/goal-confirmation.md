# Gateflow Goal Confirmation — Sell Put Top1 W5 Evaluator Slice

- Gate: `goal confirmation`
- Work unit: `sell-put-top1-w5-evaluator`
- Confirmed by user: 2026-08-15
- Branch: `feat/sell-put-top1-w5`
- Base: `origin/main@6b16fb3d189e68ba9957de3a60cfa59cc89a0294`
- Artifact path: `docs/gateflow/sell-put-top1-w5-evaluator/goal-confirmation.md`
- Decision: accepted

## Goal and motivation

Implement the smallest pure W5 research evaluator that can consume one already frozen W4 40-day dataset in memory, rerank the baseline and every authorized ranking variant through the existing Candidate Engine seam, reuse the existing expiry economics and paired-day statistics, and deterministically produce one of:

- a unique `research_leader`;
- `no_research_winner`;
- `insufficient_evidence`.

This slice exists so the core parameter-selection logic can be proved with synthetic sealed facts while the real `HK/lx` provider/readiness path remains unavailable.

## Success signals

- One hand-checkable synthetic 40-trading-day case proves T0 `sell_limit` counterfactual semantics, exact-expiration close use, terminal fee use, multiple points per day, daily aggregation, statistics, and unique leader selection.
- Baseline and all ExperimentSpec variants reuse `rerank_recommendation_point()`, `calculate_expiry_efficiency()`, and `summarize_paired_daily_deltas()`; no ranking or formula is copied.
- Leader selection is exactly mean descending, one-sided lower bound descending, worst-tail mean descending, then `variant_id` ascending.
- No-winner, deterministic tie-break, missing exact close, incomplete fee evidence, empty/same-Top1 evidence, and concentration non-increase failure are deterministic.
- A real W4 `sealed_historical_dataset.v1` shape and its referenced projections can be materialized into the pure evaluator without a new provider or storage abstraction.
- The returned leader can enter the existing M3 `lock_challenger()` boundary only as the exact system leader; validation remains `unconfirmed` until a separate human authorization.

The existing W1B statistics suite remains the owner of the generic `hard_risk_violation` and `risk_evidence_missing` branches. A valid W4 projection contains only the producer-accepted universe, so this evaluator supplies `hard_risk_status=passed` rather than inventing a second risk receipt.

## Scope boundary

This work unit adds only the pure evaluator and executable synthetic seams. It does not implement the original W5 provider runner and must not be described as completing W5 as a whole.

Explicitly deferred:

- `run_research()` orchestration;
- filesystem loading/publication and SQLite research revision/terminal writes;
- Futu/OpenD history K-line or quota calls;
- a real fee-plan, calendar, close, quota, or capacity receipt;
- a real 40-day research run;
- live fill observation, outcome jobs, 20-day hidden validation, timer/service/CLI/Agent/LLM integration;
- release, deployment, production config, notification, or market/broker write.

## Direct code evidence

- W4 already freezes `sealed_historical_dataset.v1` as a reference-only 40-day manifest in `corpus.py`; it intentionally does not load provider outcomes.
- W1A already validates and reranks a projection with Candidate Engine in `ranking.py`.
- W1B already owns canonical expiry economics in `economics.py` and paired daily statistics in `statistics.py`.
- M3 already enforces immutable research/validation hashes, exact system-leader locking, separate human validation authorization, and terminal publication in `lifecycle.py` plus `ExperimentStore`.
- `docs/performance/sell-put-top1-capability-preflight-20260814.md` remains `W0R runtime_no_go`; there is no local OpenD listener on port 11111 in this worktree preflight.

## Parsimony decision

Add one production module and one focused test module. Do not add a provider interface, loader class, repository, schema migration, workflow state, generic receipt framework, public CLI, or a second statistics/ranking implementation. The future runner will perform artifact I/O and sealing when runtime readiness is green.

## Blocking open questions

None. The user explicitly confirmed this narrowed evaluator-only boundary.

## Next gate

`plan`
