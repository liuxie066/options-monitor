# Gateflow Goal Confirmation — Sell Put Top1 W1B

- Gate: `goal confirmation`
- Work unit: `sell-put-top1-w1b`
- Branch: `feat/sell-put-top1-w1b`
- Planning base: `origin/main@8528de6b59f89b815c9b481a69bfa6055333b93a`
- Required implementation base:
  - W1A: `feat/sell-put-top1-w1a@2feecf1188c4cc1d1296d54bddfb8027f1ed5e36` / PR #156
  - HK terminal fee contract: `feat/sell-put-top1-hk-terminal-fee-contract@1c899df8426edc945ab5278041b876260ef395f8` / PR #157
- Design documents: the three Sell Put Top1 plans committed by W1A at `2feecf11`
- Confirmation: user confirmed the W1B target and boundary on 2026-08-15.
- Artifact path: `docs/gateflow/sell-put-top1-w1b/goal-confirmation.md`

## Goal and motivation

Implement the smallest deterministic core that later research and validation modules can share: strict experiment-spec semantics, a versioned behavior hash, expiry economic efficiency, paired day aggregation, and dynamic one-sided Student-t statistics. This module turns already sealed facts into reproducible metrics; it does not collect, persist, authorize, schedule, or explain experiments.

## Success signals

- `ExperimentSpec` accepts only the confirmed first ranking hypothesis and exact v1 constants; unknown keys, filtering patches, caller-supplied behavior hashes, and validation-only placeholders fail closed.
- Research and validation semantic projections produce stable canonical hashes; provenance-only fields never affect behavior/spec hashes, while every behavior-owning contract version does.
- Expiry PnL and annualized capital efficiency match hand calculations; W1B binds strike/multiplier/one-contract inputs by calling the canonical terminal-fee calculator and fails closed when account fee-plan evidence is incomplete.
- Multiple recommendation points are averaged within account/trading day, then days are equally weighted.
- Sample standard deviation, standard error, dynamic one-sided Student-t lower bound, and `ceil(n * worst_fraction)` tail metrics match hand calculations for both 20- and 40-day fixtures.
- Deterministic decision ordering produces only the confirmed outcomes/reason codes owned by this pure module.
- Focused tests, architecture guard, dependency checks, and Kimi DeepReview have no unresolved accepted finding.

## Scope boundary

### Included

- `sell_put_top1_experiment_spec.v1` validation and fixed research/validation spec projections.
- `sell_put_top1_behavior_binding.v1` canonical hash.
- Pure expiry PnL/efficiency calculation from complete sealed facts through the versioned terminal-fee calculator.
- Pure point/day paired delta aggregation and summary statistics.
- Deterministic statistical/risk gate decision from already supplied complete evidence.
- SciPy on the existing runtime requirements/constraints path, pinned once through the repository's canonical constraints.

### Excluded

- Experiment IDs, authorization, phase/progress state, hidden commitments, persistence, migrations, CAS, manifests, filesystem, clocks, retries, or recovery.
- Recommendation-point publishing, corpus capture/freezing, OpenD/provider reads, fill observation, outcome jobs, research leader orchestration, or validation lifecycle.
- Candidate filtering/ranking changes, expanded candidate universe, production configuration, CLI/service/timer, Agent tools, Prompt/LLM, GitHub issues, release, deployment, or a real experiment.
- A generic experiment DSL, contract registry, dependency graph, statistics backend abstraction, or fallback implementation.

## Direct code evidence

- W1A exports the authoritative `SELL_PUT_RANKING_CONTRACT_VERSION` and `RANKING_PROJECTION_SCHEMA_VERSION`; W1B must consume those constants instead of duplicating ranking semantics.
- `domain/domain/decision_state_fingerprint.py::canonical_sha256()` already owns canonical hashing and is reusable without a new hashing framework.
- PR #157 exports `FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION` and `calc_futu_hk_terminal_fee()`; incomplete fee results already carry `complete=false` and must suppress net efficiency.
- Python's standard library has no Student-t inverse CDF. The product contract explicitly requires `scipy.stats.t.ppf` and forbids a fixed critical value or normal fallback.

## First-principles and overdesign judgment

The work unit exists because research and hidden validation must use identical economic/statistical semantics without importing either workflow. Three small pure modules are sufficient. W1B adds no state machine, storage boundary, generic framework, backend interface, or future hypothesis type.

## Branch and dirty-worktree ownership

- The root W1A worktree contains unrelated tracked and untracked user changes and remains untouched.
- Planning occurs in the isolated worktree `/private/tmp/options-monitor-sell-put-top1-w1b-20260815`.
- Code implementation must not start until PR #156 and PR #157 are explicitly authorized and merged into `main`, then this branch is rebased or recreated from that merged base. A synthetic integration branch is not allowed.

## Blocking open questions

- Implementation-base transition requires explicit user authorization to mark PR #156/#157 ready and merge them. This does not block the plan/PlanReview gates, but it blocks accepted-plan implementation.

## Residual risks

- Real HK/lx account fee-plan and provider readiness remain W0R concerns; W1B only proves deterministic behavior for complete synthetic inputs.
- Research leader selection across multiple variants belongs to W5; W1B only summarizes one paired baseline/challenger series.
- Hidden-window isolation and authorization invalidation belong to W3/W6; W1B only returns hashes and pure decisions.

## Decision

`goal-confirmation-pass`; next gate: `plan`.
