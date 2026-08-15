# Gateflow Plan — Sell Put Top1 W1B

- Gate: `plan`
- Work unit: `sell-put-top1-w1b`
- Branch: `feat/sell-put-top1-w1b`
- Planning base: `origin/main@8528de6b59f89b815c9b481a69bfa6055333b93a`
- Required implementation base: merged PR #156 and PR #157 on `main`
- Design source: Sell Put Top1 product, modular technical, and implementation-control plans at W1A commit `2feecf11`
- Artifact path: `docs/gateflow/sell-put-top1-w1b/plan.md`
- Current gate: `plan`
- Next entry point: `plan review`

## Goal, motivation, and success signal

Deliver the pure contract/economic/statistical core shared by later 40-day research and 20-day hidden validation. It must turn complete sealed facts into canonical spec hashes, expiry efficiency, daily paired deltas, and a deterministic metric-gate result without owning any workflow or I/O.

The work unit passes when strict spec/hash fixtures, hand-calculated economics, 20/40-day Student-t fixtures, failure paths, architecture guards, clean dependency installation, focused/full tests, and all required Kimi DeepReview loops pass.

## Non-goals and scope boundary

- No ranking/filter change, accepted-set expansion, Candidate Engine policy copy, or production behavior change.
- No research leader selection across variants; W1B summarizes one baseline/challenger series and W5 selects a leader.
- No final experiment phase/outcome mapping; W1B returns `pass | keep_baseline | insufficient_evidence`, W5/W6 own lifecycle-specific outcomes.
- No SQLite, files, manifests, clocks, retries, authorization, hidden commitments, corpus, OpenD, fill monitoring, outcome jobs, CLI, timer/service, Agent tools, Prompt/LLM, release, deploy, or real experiment.
- No generic experiment DSL, registry, dependency graph, provider interface, statistics backend abstraction, normal approximation, or hard-coded t table.

## Design alignment and direct code evidence

- W1A owns `SELL_PUT_RANKING_CONTRACT_VERSION`, `SELL_PUT_RANKING_PROFILES`, and `RANKING_PROJECTION_SCHEMA_VERSION`; W1B imports those exact constants.
- `OPENING_CANDIDATE_SNAPSHOT_SCHEMA` already owns the opening fact schema.
- `canonical_sha256()` already provides repository-wide canonical hashing; W1B reuses it.
- PR #157 owns `FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION` and the structured terminal-fee result. W1B consumes the result and never recreates a fee schedule.
- The repository requires Python 3.12. SciPy 1.18.0 supports Python 3.12 and is the current stable release verified from PyPI on 2026-08-15. The existing install path is `requirements.txt -> requirements/runtime.txt` plus top-level `constraints.txt`.

## Stable constants

`contracts.py` owns only constants that have no existing owner:

```text
EXPERIMENT_SPEC_SCHEMA_VERSION = sell_put_top1_experiment_spec.v1
BEHAVIOR_BINDING_SCHEMA_VERSION = sell_put_top1_behavior_binding.v1
ACCEPTED_SET_CONTRACT_VERSION = same_point_producer_accepted_set.v1
RESEARCH_SELECTION_CONTRACT_VERSION = sell_put_top1_research_selection.v1
RESEARCH_METRIC_CONTRACT_VERSION = counterfactual_expiry_efficiency.v1
VALIDATION_FILL_CONTRACT_VERSION = scheduled_point_first_observed_cross.v1
VALIDATION_METRIC_CONTRACT_VERSION = sell_put_top1_paired_daily_efficiency.v1
EXPIRY_OUTCOME_CONTRACT_VERSION = expiry_outcome_at_underlier_close.v1
```

Existing owners remain authoritative for opening snapshot, ranking projection, Sell Put ranking, and HK terminal-fee versions.

## Public contracts

### 1. Experiment spec and hashes

Add to `src/application/strategy_lab/top1/contracts.py`:

```python
class Top1CoreContractError(ValueError):
    reason_code: str

validate_experiment_spec(payload) -> dict
build_behavior_binding(contract_versions) -> str
build_research_spec_sha256(validated_spec) -> str
build_validation_spec_sha256(
    validated_spec,
    *,
    research_terminal_sha256,
    challenger_variant_id,
    hidden_window_commitment_sha256,
) -> str
```

The ExperimentSpec validators require exact key sets, reject booleans where numbers are expected, reject non-finite numbers, and return a detached plain-data copy. Malformed ExperimentSpec input raises `Top1CoreContractError` with `reason_code="experiment_spec_invalid"`. Economics/statistics use ordinary `ValueError` for malformed programmer input; evidence-level outcomes remain structured results.

#### Behavior binding

`contract_versions` has exactly these keys:

```text
baseline_version
opening_snapshot_schema_version
accepted_set_contract_version
ranking_projection_schema_version
sell_put_ranking_contract_version
research_selection_contract_version
research_metric_contract_version
validation_fill_contract_version
validation_metric_contract_version
fee_schedule_version
market_calendar_version
expiry_outcome_contract_version
```

The function hashes exactly:

```json
{
  "schema_version": "sell_put_top1_behavior_binding.v1",
  "baseline_version": "...",
  "opening_snapshot_schema_version": "...",
  "accepted_set_contract_version": "...",
  "ranking_projection_schema_version": "...",
  "sell_put_ranking_contract_version": "...",
  "research_selection_contract_version": "...",
  "research_metric_contract_version": "...",
  "validation_fill_contract_version": "...",
  "validation_metric_contract_version": "...",
  "fee_schedule_version": "...",
  "market_calendar_version": "...",
  "expiry_outcome_contract_version": "..."
}
```

Every value must be non-empty canonical text. The generic hash builder permits a changed non-empty version so drift tests can prove every field affects the digest. `validate_experiment_spec()` separately requires all current v1 owner constants and recomputes, rather than trusting, `baseline.behavior_binding_sha256`.

Git/source SHA, account config/policy hashes, dataset/ref, timer revision, Prompt/model, timestamps, topic/experiment/account identity, and runtime state are not accepted by this function.

#### ExperimentSpec shapes

The validator supports exactly two shapes. The research-ready shape has these top-level keys:

```text
schema_version, topic_id, experiment_id, market, account,
hypothesis, baseline, research_source, research_evaluation,
variants, frozen_safety, economics_contracts, expiry_outcome
```

The validation-ready shape adds all four keys as one atomic group:

```text
validation_evaluation, fill_observation, timer_binding, validation_metrics
```

Partial presence of that group fails closed. No `initial_*` provenance, authorization, actor/time, hash-result, hidden-window, research-terminal, challenger, or lifecycle field is accepted into the ExperimentSpec itself.

Nested contracts:

- `schema_version`: exact current spec version.
- `topic_id`, `experiment_id`: non-empty trimmed text.
- `market`: exact `HK`; `account`: non-empty lowercase canonical text. W3/W7 later enforce the first live `lx` opt-in; this pure contract does not grant account authority.
- `hypothesis`: exact keys `hypothesis_type`, `statement`, `mechanism`, `independent_variable`, `expected_direction`; fixed values are `sell_put_ranking`, `cross_symbol_concentration_priority`, and `higher_top1_efficiency_without_higher_concentration`, with non-empty statement/mechanism.
- `baseline`: exact keys `version`, `opening_snapshot_schema`, `accepted_set_contract_version`, `ranking_projection_schema_version`, `sell_put_ranking_contract_version`, `behavior_binding_sha256`; all versions equal their current owners and the hash equals the recomputed behavior binding.
- `research_source`: exact keys `mode`, `dataset_ref`, `dataset_sha256`, `research_cutoff_at`, `start_trading_date`, `end_trading_date`; mode is `sealed_historical_dataset`, ref is a safe relative POSIX path, digest is lowercase SHA-256, cutoff is UTC `Z`, and ISO dates satisfy start <= end. W4 owns calendar continuity/maturity.
- `research_evaluation`: exact fixed values `contract_version`, `metric_contract_version`, `fill_assumption=t0_sell_limit`, `required_days=40`, `window_mode=fixed_consecutive_trading_days`, `visibility=visible_after_research_seal`.
- `validation_evaluation`: exact fixed values `required_days=20`, `window_mode=fixed_future_consecutive_trading_days`, `visibility=hidden_until_final_seal`.
- `variants`: ordered list with at least baseline plus one level, beginning with exactly `{"variant_id":"baseline","patch":{}}`; remaining IDs are unique non-empty text and each patch has exactly `ranking_profile`, limited to the full W1A `SELL_PUT_RANKING_PROFILES` set: `without_concentration | current_tie_break | concentration_first`. Duplicate profiles are rejected. `current_tie_break` is retained as the approved baseline-equivalent control arm; its zero delta makes it unable to pass the positive-improvement gate.
- `frozen_safety`: exact `mode=inherit_each_point_producer_accepted_set` and `variant_may_change_acceptance=false`.
- `fill_observation`: exact `applies_to=validation_only` and current validation-fill contract.
- `economics_contracts`: exact `fee_schedule_version` equal to the current HK terminal-fee schedule and non-empty `market_calendar_version`.
- `timer_binding`: exact keys `revision`, `producer_catchup_grace_seconds`, `producer_run_timeout_upper_bound_seconds`, `advance_cadence_seconds`, `fill_observation_duration_upper_bound_seconds`, `terms_capture_duration_upper_bound_seconds`; revision is non-empty and every duration is a positive integer. W7 readiness proves the installed values and cross-field budgets.
- `expiry_outcome`: exact current contract plus `spot_source=opend_history_kline`, `ktype=K_DAY`, `autype=NONE`, `price_field=close`, `due_boundary=expiration_observation_start_ms`, `pending_elapsed_hours=72`.
- `validation_metrics`: exact current contract, `confidence_level=0.95`, `worst_fraction=0.20`.

`build_research_spec_sha256()` revalidates either supported ExperimentSpec shape, projects the research subset, and hashes only:

```text
schema_version, hypothesis, baseline, research_source,
research_evaluation, variants, frozen_safety,
economics_contracts, expiry_outcome
```

It excludes topic/experiment/market/account identity and all validation-only fields exactly as the product contract states.

`build_validation_spec_sha256()` requires a validation-ready spec, a lowercase SHA-256 research terminal, a non-baseline challenger ID present in `variants`, and a lowercase SHA-256 hidden commitment. It hashes exactly:

```text
schema_version, research_terminal_sha256, challenger_variant_id,
hidden_window_commitment_sha256, validation_evaluation,
fill_observation, economics_contracts, timer_binding,
expiry_outcome, validation_metrics
```

No function writes the resulting hashes into the input or authorizes a phase.

### 2. Expiry economics

Add to `src/application/strategy_lab/top1/economics.py`:

```python
calculate_expiry_efficiency(economic_facts) -> dict
```

The function accepts one of two exact shapes.

No observed validation fill:

```json
{"stage":"validation","fill_status":"no_observed_fill"}
```

It returns an evaluable result with `economic_pnl=0.0`, `efficiency=0.0`, no holding period, and no terminal-fee claim.

Research assumed fill or validation observed fill:

```text
stage
fill_status
holding_start_date
expiration
opening_net_premium
net_cash_basis
strike
multiplier
underlier_close
account_fee_plan
```

Rules:

- `research` requires `fill_status=t0_assumed_fill`; `validation` requires `observed_fill`.
- Dates are canonical ISO dates; holding days are `(expiration - holding_start_date).days` and must be positive.
- Premium, basis, strike, multiplier, and close are finite numbers; premium/basis/strike/close are positive and multiplier is a positive integer.
- `account_fee_plan` is null or a mapping whose keys are a subset of `commission_free`, `platform_fee`, and `fee_plan_ref`; extra keys are rejected. A complete plan has all three correctly typed facts: boolean commission flag, finite non-negative platform fee, and non-empty canonical ref. Null, partial, or incorrectly typed allowed facts are passed to the canonical calculator as missing evidence and never converted to a default.
- `intrinsic_per_share=max(strike-underlier_close, 0)`. Positive intrinsic selects `assignment`; zero intrinsic selects `expired_worthless`.
- W1B calls the canonical owner itself: `calc_futu_hk_terminal_fee(kind, order_price=strike, shares=multiplier, contracts=1, account_fee_plan=account_fee_plan)`. The caller cannot inject an amount, kind, shares, order price, or contract count.
- The returned fee must be `HKD` and use the current HK terminal-fee schedule. A complete amount must be finite/non-negative and `basis=estimated`.
- A canonical fee result with `complete=false` returns `status=not_evaluable`, `reason_code=required_outcome_missing`, `reason_detail=expiry_fee_unavailable`, and null net metrics. It never uses `estimated_amount` as a substitute.
- Non-positive holding duration returns `status=not_evaluable`, `reason_code=holding_period_non_positive`, and null net metrics.
- Expired-worthless PnL is `opening_net_premium - terminal_fee.amount`.
- Assignment-proxy PnL is `opening_net_premium + (underlier_close - strike) * multiplier - terminal_fee.amount`.
- Efficiency is `economic_pnl / net_cash_basis / holding_calendar_days * 365`.
- Intermediate values are not rounded.

The output has exact keys:

```text
status, reason_code, reason_detail, stage, fill_status,
assignment_proxy, intrinsic_per_share, holding_calendar_days,
terminal_fee_schedule_version, terminal_fee_reason,
terminal_fee_amount, economic_pnl, efficiency
```

Structural/type/contract mismatch raises `ValueError`; missing complete fee evidence and non-positive holding duration are data outcomes, not exceptions.

### 3. Paired daily statistics

Add to `src/application/strategy_lab/top1/statistics.py`:

```python
summarize_paired_daily_deltas(point_rows, policy) -> dict
```

`policy` has exactly:

```text
required_days: integer >= 2
confidence_level: finite 0 < x < 1
worst_fraction: finite 0 < x <= 1
require_concentration_non_increase: bool
```

ExperimentSpec v1 supplies 40/20, 0.95, and 0.20; the pure formula accepts another valid `required_days` so the t critical and tail size remain mathematically linked to actual `n` rather than hidden constants.

Each call summarizes one already-bound account; account is intentionally not repeated in point rows. Each point row has exactly:

```text
recommendation_point_id
trading_date
baseline_candidate_id
challenger_candidate_id
baseline_efficiency
challenger_efficiency
hard_risk_status
baseline_concentration
challenger_concentration
```

Point IDs are unique, dates are canonical ISO dates, candidate IDs are both non-empty or both null, risk status is `passed | violated | missing`, and numeric values are finite when present.

Point semantics:

- Same non-null candidate: delta `0.0`; outcome values are not needed because the decision is identical.
- Different non-null candidates: both efficiencies are required; delta is challenger minus baseline.
- Both candidates null: `no_evidence`, no delta.
- One-sided candidate or a different-candidate row missing either efficiency: result is `insufficient_evidence` with `official_decision_incomplete`.
- If concentration non-increase is required, every different-candidate comparable point requires both concentration values; missing evidence yields `risk_evidence_missing`, any challenger value above baseline yields `concentration_non_increase_failed`.

Daily/statistical semantics:

- Group valid point deltas by trading date and take the arithmetic mean per day; every point and every resulting day are equally weighted.
- A day containing only both-no-candidate rows has no daily delta; the window is never extended.
- Duplicate point IDs or more distinct trading dates than `required_days` are contract errors.
- If any hard-risk status is `violated`, return `keep_baseline/hard_risk_violation` before other gates.
- Otherwise missing risk/point evidence, or effective days below required, returns `insufficient_evidence` with the specific reason; do not run a smaller-n significance test.
- For a complete window use `statistics.stdev()` (sample `n-1`), `se=s/sqrt(n)`, and lazy `scipy.stats.t.ppf(confidence_level, df=n-1)`.
- Missing/import-failed/non-finite SciPy output returns `insufficient_evidence/statistics_backend_unavailable`; there is no fallback.
- If sample standard deviation is exactly zero, lower bound equals mean.
- `worst_k=ceil(n*worst_fraction)` and the tail mean uses the smallest `worst_k` daily deltas.
- Decision order after complete evidence is: non-positive mean -> `keep_baseline/non_positive_mean`; negative tail -> `keep_baseline/negative_worst_tail`; positive mean but lower bound <= 0 -> `insufficient_evidence/positive_mean_lcb_not_above_zero`; otherwise `pass` with `positive_one_sided_lcb`, `non_negative_worst_tail`, and `hard_risk_passed`.
- A concentration-non-increase failure is evaluated after all required point/risk evidence is complete and before the mean/tail/LCB gates.
- The output always states `serial_correlation_unadjusted=true`.

The output has exact keys:

```text
decision, reason_codes, required_days, effective_days,
point_results, daily_deltas, mean_daily_delta, sample_std,
standard_error, t_critical, one_sided_lower_bound,
worst_k, worst_tail_mean, serial_correlation_unadjusted
```

Pre-statistics exits keep statistical fields null. The LCB-only insufficient result retains all calculated statistics.

`point_results` preserves input order and contains exact rows:

```text
recommendation_point_id, trading_date, status, point_delta
```

`status` is `same_candidate | paired | no_evidence`; one-sided or missing different-arm evidence exits with `official_decision_incomplete` and no partial result list. `daily_deltas` is trading-date sorted and contains exact rows:

```text
trading_date, effective_point_count, daily_delta
```

Only days with at least one point delta appear. `reason_codes` order is deterministic: the single failing gate code for non-pass results; the three documented codes in order for `pass`.

## Dependency decision

- Add `scipy` to `requirements/runtime.txt`.
- Add `scipy==1.18.0` to both `constraints/runtime.txt` and the top-level `constraints.txt`, because the installer passes the latter directly and it does not include the former.
- Do not create an optional research profile or a backend wrapper.
- Verify a clean Python 3.12 install can import SciPy before accepting the slice.

## Affected files and allowed changes

### Production

- `src/application/strategy_lab/top1/contracts.py`
- `src/application/strategy_lab/top1/economics.py`
- `src/application/strategy_lab/top1/statistics.py`
- `requirements/runtime.txt`
- `constraints/runtime.txt`
- `constraints.txt`

`src/application/strategy_lab/top1/__init__.py` stays empty; callers import the owning module directly, avoiding a package facade and circular imports.

### Tests and generated architecture evidence

- `tests/test_strategy_lab_top1_w1b.py`: all strict contract, economics, statistics, and failure fixtures.
- `tests/test_strategy_lab_top1_architecture.py`: add the exact approved imports for the three W1B modules.
- `docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md`: only the reviewed canonical `market=HK` correction and one casing sentence.
- `docs/dependency_graph.mmd` and `docs/DEPENDENCY_GRAPH.md`: regenerate only if the repository checker requires it.
- Gateflow and timestamped PlanReview/DeepReview artifacts for this work unit.

No other file is allowed without a new Gateflow scope decision.

## Import boundary

```text
contracts.py
  -> stdlib
  -> domain.domain.decision_state_fingerprint
  -> domain.domain.engine
  -> domain.domain.fee_calc
  -> src.application.opening_candidate_snapshot
  -> src.application.strategy_lab.top1.ranking

economics.py
  -> stdlib
  -> domain.domain.fee_calc

statistics.py
  -> stdlib
  -> scipy.stats (lazy, only for ppf)
```

None may import infrastructure, interfaces, filesystem/pathlib, SQLite, OpenD, config, service, Agent/LLM, research, validation, or lifecycle modules. Candidate Engine and fee owners never import Strategy Lab.

## Implementation slice

### W1B-S1 — pure spec/economics/statistics core

- Objective: implement every public contract and fixture in this plan as one cohesive pure module work unit.
- Prerequisites: accepted plan; PR #156 and #157 merged into `main`; W1B branch rebased/recreated from that exact main; clean dependency install available.
- Allowed files: exactly those listed above.
- Exact changes: three pure modules, one focused test file, one architecture-test extension, three dependency lines, and mechanically generated dependency docs if required.
- Non-goals: every workflow/I/O capability and all later W2–W9 behavior.
- Stop conditions: missing prerequisite contract, product-doc ambiguity that changes a schema/hash/result, need to modify Candidate Engine or fee owner, dependency installation failure, or any forbidden import.
- Completion signal: focused and full validation pass, Kimi code review/fix/re-review has no unresolved accepted finding, accepted slice commit exists, then aggregate/PR Kimi gates run before final closeout.

One slice is intentional: the three files form one public pure contract and share one fixture matrix. Splitting dependency/spec/economics/statistics into separate review commits would create unusable intermediate states without reducing implementation risk.

## Tests and exact assertions

`tests/test_strategy_lab_top1_w1b.py` must prove:

- Research-ready and validation-ready golden specs validate; every missing/extra key, partial validation group, wrong fixed constant, filter/hard-risk patch, duplicate profile, bad hash/path/date/time/number/case, caller-forged behavior hash, and timer zero fails closed. All three W1A ranking profiles are accepted as unique research levels, including the baseline-equivalent `current_tie_break` control.
- Behavior hash is stable under source/config/policy/dataset/timer/Prompt provenance changes because those keys are outside its accepted domain; changing each of the twelve behavior fields changes the digest.
- Research hash ignores topic/experiment/account and validation-only fields, but changes for every research semantic field.
- Validation hash changes for research terminal, challenger, hidden commitment, fill/metrics/economics/outcome/timer semantics; it rejects a baseline or unknown challenger.
- Expired-worthless, assignment loss/profit, no observed fill, incomplete account fee plan, canonical fee-version mismatch, and non-positive holding period match hand calculations and statuses. A spy around `calc_futu_hk_terminal_fee()` proves W1B binds `order_price=strike`, `shares=multiplier`, and `contracts=1`; there is no caller-supplied fee amount.
- Multiple point deltas average within day; both-no-candidate produces no day; same Top1 produces zero; single-sided and missing different-arm values fail closed.
- Hand-computed 20- and 40-day fixtures match mean, sample standard deviation, standard error, known `df=19/39` one-sided 0.95 t quantiles, LCB, `ceil(n*0.20)`, and worst-tail mean.
- Zero variance gives LCB equal to mean; a second valid `required_days` proves no `1.729`/four-day constant.
- Hard-risk violation precedence, risk missing, incomplete days, non-positive mean, negative tail, positive-mean/non-positive-LCB, concentration failure, successful pass, and SciPy-unavailable behavior produce the exact decisions/reasons.
- Inputs are not mutated.

## Validation commands

After the merged-base transition and dependency installation:

```bash
./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_strategy_lab_top1.py \
  tests/test_strategy_lab_top1_w1b.py \
  tests/test_strategy_lab_top1_architecture.py \
  tests/test_candidate_engine_contract.py \
  tests/test_candidate_engine_parity.py \
  tests/test_fee_calc.py

./.venv/bin/ruff check \
  src/application/strategy_lab/top1 \
  tests/test_strategy_lab_top1_w1b.py \
  tests/test_strategy_lab_top1_architecture.py

./.venv/bin/python -m basedpyright \
  src/application/strategy_lab/top1/contracts.py \
  src/application/strategy_lab/top1/economics.py \
  src/application/strategy_lab/top1/statistics.py

./.venv/bin/python scripts/generate_dependency_graph.py --check
./.venv/bin/python -m pip check
./.venv/bin/python -c 'import scipy; assert scipy.__version__ == "1.18.0"'
./.venv/bin/python -m pytest -q -p no:cacheprovider
git diff --check
```

Clean-install proof uses a temporary Python 3.12 virtual environment and the exact public install path:

```bash
python3.12 -m venv <temporary-directory>/.venv
<temporary-directory>/.venv/bin/pip install -r requirements.txt -c constraints.txt
<temporary-directory>/.venv/bin/python -c 'import scipy; assert scipy.__version__ == "1.18.0"'
```

Dependency download/install is a local development-environment mutation and will be requested separately when implementation begins. It is not a release or deployment.

## Review and commit sequence

- PlanReview must pass; fixes are re-reviewed before the accepted-plan commit.
- W1B-S1 implementation is followed immediately by Kimi DeepReview. Every accepted finding is fixed and re-reviewed before the accepted-slice commit.
- Gateflow aggregate DeepReview and PR DeepReview also use Kimi, with the same zero-unresolved-finding rule.
- Draft PR creation is allowed by Gateflow; marking ready, merging, releasing, deploying, service changes, and real experiments remain separately authorized.

## Documentation decision

Apply one material design-source correction after the prerequisite merge: change the ExperimentSpec JSON example's `market` from lowercase `hk` to canonical producer value `HK` and state that account, not market, is lowercase. Do not otherwise rewrite the product or modular plans. Gateflow artifacts record W1B's exact executable contract; dependency graph documents are mechanical outputs only.

## Risks and classification

- PR #156/#157 are not merged: blocking prerequisite before accepted-plan implementation; explicit user authorization required.
- Real fee-plan/provider evidence: assigned to W0R and later W5/W6; synthetic W1B tests do not claim runtime readiness.
- Research leader tie-breaking: assigned to W5; not duplicated here.
- Hidden-window/auth invalidation: assigned to W3/W6; hash builders have no write authority.
- Serial correlation: intentionally unadjusted in the approved v1 contract and always disclosed.
- SciPy availability: fixed in current slice through the existing runtime install path; missing backend fails closed.

## Completion report format

- Accepted plan/slice/deepreview/PR-review/final-closeout commit hashes.
- Changed files and stable public contracts exposed to W3/W5/W6.
- Focused/full tests, Ruff, type check, dependency graph, pip check, clean-install evidence, and exact SciPy version.
- Kimi artifacts and final finding statuses.
- Draft PR URL and classified residual risks.
- Explicit statement that no merge/release/deploy/config/service/provider/production write occurred.

## Why this is not overdesigned

W1B adds three pure files because spec semantics, money calculation, and statistical aggregation have different inputs and failure rules. It reuses canonical hashing, Candidate Engine versions, and the terminal-fee owner; adds one required numerical dependency; and creates no state, I/O, registry, facade, interface hierarchy, or future capability shell.
