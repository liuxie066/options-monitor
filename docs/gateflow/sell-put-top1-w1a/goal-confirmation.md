# Gateflow Goal Confirmation — Sell Put Top1 W1A

- Gate: `goal confirmation`
- Work unit: `sell-put-top1-w1a`
- Branch: `feat/sell-put-top1-w1a`
- Design documents:
  - `docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md`
  - `docs/plans/sell-put-top1-modular-technical-implementation-plan-20260814.md`
  - `docs/plans/sell-put-top1-modular-implementation-control-20260814.md`
- Confirmation: user confirmed the branch and W1A boundary on 2026-08-15.
- Artifact path: `docs/gateflow/sell-put-top1-w1a/goal-confirmation.md`

## Goal and motivation

Implement the first deterministic Top1 module: the three approved Sell Put concentration ranking profiles and a strict, replayable projection of the producer-accepted candidate set. This creates the smallest stable contract needed by later point capture, corpus, research, and validation modules while preserving Candidate Engine as the sole ranking authority.

## Success signals

- Calls that omit a profile preserve the current production Sell Put and Covered Call order exactly.
- The three approved Sell Put profiles produce the specified cross-symbol ordering and cannot change the accepted set.
- A sealed opening snapshot can be projected into the minimal `sell_put_ranking_projection.v1` facts and reranked without filesystem, clock, SQLite, OpenD, config, service, or LLM access.
- Missing or invalid projection facts fail closed with a stable reason; default-profile divergence from the producer order fails with `baseline_rank_parity_mismatch`.
- Focused tests, architecture guard, Ruff, dependency graph, and available type checks pass before review.
- Kimi DeepReview findings are fixed and re-reviewed before the work unit advances.

## Scope boundary

### Included

- Candidate Engine ranking profile contract and default-compatible implementation.
- Strict ranking projection builder/validator and deterministic reranking result.
- Focused parity/profile/projection/architecture tests.
- Narrow plan corrections that distinguish source-build readiness from real provider/pilot readiness.

### Excluded

- Experiment lifecycle, SQLite, corpus persistence, recommendation-point publishing, research/validation economics or statistics.
- HK terminal-fee behavior, OpenD provider receipts, SciPy, service/timer/CLI, Agent tools, Prompt/LLM integration.
- Filter parameters, expanded candidate universe, production configuration, release, deployment, service installation, or a real experiment.

## Direct code evidence

- `domain/domain/engine/candidate_engine.py::rank_candidate_rows()` already owns the formal within-symbol and cross-symbol ordering.
- `src/application/opening_candidate_snapshot.py` seals accepted decision IDs, normalized inputs, producer ranks, and content hashes; W1A can consume those facts without a second metrics or policy implementation.
- The current W0 artifact is `no-go` for a real HK/lx pilot because provider/account evidence is incomplete, but those live receipts are not inputs to a pure ranking/projection implementation.

## First-principles and overdesign judgment

The work unit is justified because every later module needs a versioned ranking seam and a source-deletion-safe minimal projection. It deliberately does not add a registry, repository interface, workflow engine, generic parameter DSL, second Candidate Engine, or speculative expanded-universe contract.

## Dirty-worktree ownership

The following existing changes are excluded and must remain unstaged and unmodified by this work unit:

- `AGENTS.md`
- `docs/plans/data-storage-runtime-projection-phase1-contract-20260813.md`
- `domain/domain/assigned_stock.py`
- `domain/domain/fee_calc.py`
- `domain/domain/portfolio_assignment_scenario.py`
- `tests/test_assigned_stock_projection.py`
- `tests/test_fee_calc.py`
- `tests/test_portfolio_assignment_scenario.py`
- prior unrelated review artifacts

## Blocking open questions

None.

## Residual risks

- Provider/account capability gaps: covered by the approved later `W0R` remediation/readiness gate; they block real research/validation, not W1A source implementation.
- Full ExperimentSpec, fee/economic semantics, and statistical contracts: covered by later approved work units and are not prebuilt here.

## Decision

`goal-confirmation-pass`; next gate: `plan`.
