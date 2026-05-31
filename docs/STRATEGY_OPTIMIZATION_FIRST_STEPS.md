# Strategy Optimization First Steps

> Purpose: make strategy improvement evidence-driven before changing live scanner behavior.
> These steps are intentionally ordered so production config authority comes first, per-run evidence comes second, and offline learning comes third.

## P0. Runtime Config Authority

### Implementation

- Keep `config.yaml` as the human authoring source.
- Keep `config.us.json` / `config.hk.json` as generated runtime snapshots.
- Extend `runtime_status` with `config_authority`, including:
  - runtime config path, inferred market, source format, required source format
  - `config.yaml` and system source sha256 values from `_generated.sources`
  - identity/freshness check summaries
  - stale or invalid reason and rebuild command

### Boundary

- Read-only diagnostic surface only.
- Does not rebuild config.
- Does not edit `config.yaml`, `config.us.json`, or `config.hk.json`.
- Does not weaken existing runtime config identity checks in live tick/scanner paths.

### Acceptance

- `./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'` includes `data.config_authority`.
- `config_authority.ok=true` only when identity and freshness checks both pass.
- If stale or invalid, the payload includes a reason and a rebuild command.
- Tests cover the authority payload without touching production config.

## P1. Candidate Evidence Closure

### Implementation

- Keep filter evidence in `candidate_filter_trace.jsonl`.
- Extend `candidate_rank_explain` so rank explanations can target a concrete run:
  - `run_id`
  - `run_dir`
  - optional `account`
- Preserve existing direct CSV inputs:
  - `candidate_path`
  - `candidate_paths`
  - `report_dir`
  - `output_dir`

### Boundary

- Read-only.
- Does not rerun scans.
- Does not send notifications.
- Does not write reports, Feishu, positions, trade events, or runtime config.
- Ranking comparison weights only affect the explanation response.

### Acceptance

- `candidate_filter_explain` can answer why a symbol was filtered for a run/account from trace evidence.
- `candidate_rank_explain` can read `output_runs/<run_id>/accounts/<account>/...candidates...csv`.
- Response metadata lists source files and row counts.
- Tests cover run/account candidate lookup.

## P2. Offline Shadow Replay

### Implementation

- Use `src/application/shadow_replay/` as the internal module.
- Keep the implementation split by replay pipeline stage: `capture.py` owns universe/reject/rank evidence, `marking.py` owns required-data mark generation, `settlement.py` owns outcome fact derivation, `analysis.py` owns path/outcome statistics, and `readiness.py` owns the Research readiness surface. `evidence.py` is only a public facade.
- Research candidate bundles expose `candidate_evidence.shadow_replay` as a readiness surface.
- The durable replay dataset is separated into:
  - `candidate_snapshots.jsonl`: accepted, rejected, post-filtered, and ranked-below universe rows
  - `filter_decisions.jsonl`: reject/post-filter stage, rule, metric, and threshold evidence
  - `rank_snapshots.jsonl`: accepted-candidate rank explanations and score inputs
  - `mark_path_snapshots.jsonl`: later price/IV/Delta/PnL path observations
  - `outcome_facts.jsonl`: close, expiry, assignment, and counterfactual outcome facts
- `research shadow-replay mark --dataset <dataset-dir> --required-data-root output_shared/required_data --write` generates local mark path snapshots from required-data CSV quotes. Missing quotes are preserved as `missing_quote` rows and do not count as usable mark evidence; expiry spot-only marks can still support expiration settlement.
- `research shadow-replay settle --dataset <dataset-dir> --write` derives mark-to-market outcomes when price/PnL marks exist, and derives expiration outcomes such as `expired_worthless`, `assigned_at_expiry`, and `called_away_at_expiry` from spot/strike when the final mark is at or after expiration.
- Analysis may summarize DTE, Delta, IV/RV, spread, concentration, path risk, outcome stats, and outcome-by-bucket performance, but only after the universe and lifecycle evidence are explicit.

### Boundary

- Offline only.
- Reads existing report/run artifacts.
- Mark generation reads existing required-data CSV quotes and writes only the local replay dataset when `--write` is explicit.
- Defaults to `--no-write-outputs`; explicit Research output writes remain local bundle/handoff files only.
- Does not mutate scanner defaults, live config, trade state, Feishu, broker state, or notification output.
- Shadow recommendations remain advisory and require human review when samples, rejected samples, mark paths, and outcomes are sufficient.
- If Research only sees final candidate CSVs, it must flag survivorship-bias risk instead of emitting strategy conclusions.

### Acceptance

- `research collect --scope candidate` includes `candidate_evidence.shadow_replay`.
- A replay dataset can include accepted and rejected/post-filtered samples in one universe.
- At least one rejected sample can be carried into the candidate universe from trace/reject evidence.
- Required-data marks can be generated for accepted and rejected samples, while missing quotes remain evidence gaps.
- Expiration settlement can produce outcome facts from spot/strike without a live option mid when the mark is at or after expiration.
- Outcome analysis reports bucket-level PnL/win-rate by DTE, Delta, IV/RV, spread, and concentration so filter/ranking changes can be reviewed against accepted and rejected samples.
- Missing rejected samples, mark path snapshots, or outcome facts produce `not_ready` / `evidence_incomplete`, not a strategy recommendation.
- Tests cover the survivorship-bias guard: final candidate CSVs alone are evidence-incomplete.
