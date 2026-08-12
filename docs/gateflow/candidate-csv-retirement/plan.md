# Gateflow Plan — Candidate Compatibility CSV Retirement

- Work unit: `candidate-csv-retirement`
- Gate: `plan`
- Date: 2026-08-12
- Status: accepted after PlanReview re-review 3 (`pass-with-risks`)
- Branch: `refactor/candidate-csv-retirement`
- Base: `origin/main@ded8f882`
- Goal artifact: `docs/gateflow/candidate-csv-retirement/goal-confirmation.md`
- Failed PlanReview: `docs/reviews/plan-review-20260812-082422.md`
- Failed PlanReview re-review 1: `docs/reviews/plan-review-20260812-083029.md`
- Failed PlanReview re-review 2: `docs/reviews/plan-review-20260812-083433.md`
- Accepted PlanReview re-review 3: `docs/reviews/plan-review-20260812-084011.md`
- Artifact path: `docs/gateflow/candidate-csv-retirement/plan.md`

## Goal and exit contract

New US/HK candidate scans must publish and consume candidate facts through immutable account/run JSON snapshots and
the existing append-only JSONL trace only. No new candidate compatibility CSV may be written, parsed, hashed as a
canonical artifact, or used as fallback. Historical CSV-only runs stay untouched and are explicitly classified as
unsupported by automated research/replay; they are not converted into fabricated sealed snapshots.

Completion requires all of the following:

1. scheduled and manual candidate calculation is in-memory and emits no candidate/universe/reject/diagnostic/rank
   CSV, including disabled, empty, failed, and success-empty paths;
2. Combo Yield preserves its currently useful Funding Put decisions, pair diagnostics, and rank-shadow evidence in
   its sealed JSON before the CSV writers disappear;
3. strategy status, Daily Brief/Advice, research, archive, Strategy Lab, and Shadow Replay consume sealed snapshots
   and JSONL trace without any CSV fallback;
4. runs without a valid sealed candidate snapshot are classified as unsupported/incomplete rather than silently
   excluded, treated as clean `no_candidate`, or reconstructed from CSV;
5. `required_data/parsed/*_required_data.csv`, `close_advice.csv`, `symbols_summary.csv`, mark/outcome compatibility
   inputs, and unrelated explicit tabular exports keep their current contracts;
6. frozen-input tests prove candidate eligibility, rejection reasons, counts, and ranking do not change;
7. a static regression guard prevents candidate compatibility CSV names/readers/writers from returning to production
   modules.

## First-principles design

### Authority model

The plan retains the three existing strategy ownership boundaries rather than creating a generic snapshot framework:

- Sell Put / Covered Call: `opening_candidate_snapshot.json` (`opening_candidate_snapshot.v1`);
- SP+LC Combo Yield: `combo_yield_candidate_snapshot.json`, upgraded to v2;
- CC+LP: `cc_lp_candidate_snapshot.json`, upgraded to a minimal v2. Its ranked-pair payload remains unchanged because
  the CSV currently duplicates only accepted ranked pairs; v2 adds the same dependency and scope-status bindings
  required of an independent owner.

`candidate_filter_trace.jsonl` remains append-only audit evidence. It may enrich analysis, but it cannot create a
terminal candidate universe when a sealed snapshot is missing.

One narrow account-run commit marker, `candidate_snapshot_manifest.v1.json`, is added because a missing owner snapshot
cannot declare that it was expected. The manifest is published write-once only after per-symbol strategy status index
and every expected owner snapshot validate. The status index is upgraded to `strategy_scan_status_index.v2.json`; each
expected item carries `candidate_owner` (`opening`, `sp_lc`, or `cc_lp`), `strategy_mode` (`put`, `call`, or
`combo_yield`), and the account-config hash. These values are produced from the same parent-resolved config and
runtime prefilter decision that launch the symbol scan; they are not inferred later from whatever snapshot files
happen to exist. The manifest binds:

- run/account/config/policy identity and manifest content hash;
- the hash of `strategy_scan_status_index.v2.json`, whose items are the exact expected symbol/family/mode/owner scopes;
- the exact expected owner set projected from those items;
- each owner snapshot schema, relative path, content hash, opening status, and covered scopes.

For new runs, only this final manifest proves the account-run candidate set is complete. A crash after status index or
one owner snapshot but before manifest publication is explicitly incomplete and cannot become replay authority.
The manifest may lawfully commit `expected_owners=[]` and `expected_scopes=[]` when the candidate scan stage ran to
completion but runtime prefilters left no applicable scope. That state is `supported/no_applicable_scope`; it is not
`no_candidate` and not `not_scanned`.

All current-run consumers use one loader owned beside the manifest: it validates the manifest first, validates the
bound status-index and owner hashes, and only then returns an owner snapshot bundle. Daily Brief, the tick notification
flow, and AI Advice may not load an individual current-run candidate snapshot outside this gate. A missing or invalid
manifest makes all candidate evidence for that account-run unavailable; consumers do not salvage a partial owner.

### Combo Yield v2 contract

`combo_yield_candidate_snapshot.v2` is the smallest schema addition needed to replace unique CSV evidence. It binds:

- existing run/account/market/config/policy identity, terminal status, seal time, and content hash;
- dependency receipts/hashes already available at the account-run seal boundary;
- normalized per-symbol scan statuses/scope results;
- Funding Put Candidate Engine calculation and underwriting decisions;
- compact pair evaluations/diagnostics, including eligibility status, stage, rejection reasons, stable leg/pair
  identity, only the metrics needed by current rejection counts/nearest-miss analysis, and their thresholds;
- compact baseline-vs-shadow rank records keyed by stable pair id, without copying the full pair row;
- final ranked pairs.

The contract distinguishes three states rather than treating them as one set:

- `eligible`: pair passed structure and policy gates;
- `ranked_below`: eligible pair was not terminally selected;
- `selected`: pair appears in final `ranked_pairs`.

The validator checks schema, identity, hashes, JSON-safe finite normalization, list/object shapes, terminal status,
unique pair identities, and references between these states. Every selected pair must reference an eligible pair and a
baseline-selected rank record; eligible pairs may lawfully be ranked below. Publication remains write-once/adopt.
Pandas/NumPy missing values normalize to JSON `null`; non-finite numbers and unknown object types fail validation.

### Historical compatibility classification

Research/archive/replay may inspect filenames and manifests to say that a run contains legacy candidate CSV, but may
not parse candidate CSV rows. Each run is classified as one of:

- `supported`: a valid candidate snapshot manifest binds every expected owner and scope;
- `supported_limited_legacy_snapshot`: a pre-manifest run has a structurally valid v1 strategy status index, a valid
  write-once `state/config.override.json` whose canonical hash matches every consumed snapshot, and all snapshots
  required by the index after Combo symbols are mapped to SP+LC/CC+LP from that exact config; Combo v1 can provide
  only sealed ranked pairs and not v2 diagnostics, while CC+LP v1 likewise lacks the v2 dependency/scope binding;
- `unsupported_legacy_csv_only`: candidate-like CSV exists but required sealed snapshots do not;
- `unsupported_snapshot_missing`: scan evidence exists but terminal snapshot is absent;
- `unsupported_snapshot_schema`: snapshot exists but its schema/identity/hash cannot be safely consumed;
- `not_scanned`: no candidate scan evidence exists.

Classification precedence is deterministic: a present modern manifest is either `supported` or
`unsupported_snapshot_schema`; modern v2 status/snapshot evidence without a manifest is
`unsupported_snapshot_missing`; otherwise a pre-manifest v1 run is tested for the limited contract above; legacy CSV
with no valid sealed snapshot is `unsupported_legacy_csv_only`; other scan evidence without a seal is
`unsupported_snapshot_missing`; only the absence of manifest, status, snapshot, trace, and legacy candidate filename
evidence is `not_scanned`. Invalid snapshot/schema evidence takes precedence over a co-located legacy CSV name.

A requested window containing unsupported scanned runs reports incomplete coverage and keeps strict backtest/
promotion authority false. `supported_limited_legacy_snapshot` may contribute only facts from the individually valid
sealed snapshots it actually binds; it never proves modern account-run completeness, is explicitly missing pair
diagnostic coverage, and cannot by itself satisfy strict promotion. A missing/invalid legacy config authority,
unresolvable variant, owner mismatch, or expected snapshot absence is unsupported. Supported subsets may still be
summarized diagnostically, but unsupported or limited gaps are never silently dropped.

### Explicit exclusions

- Do not infer snapshot facts from old CSV bytes.
- Do not rewrite, delete, or partially prune historical runs.
- Do not move required-data, Close Advice, symbols summary, marks, or outcomes merely because they use CSV.
- Do not change strategy formulas, thresholds, ranking keys, capacity rules, or notification conclusions.
- Do not add a database, generic artifact registry, background migration, compatibility daemon, or second ranking
  implementation.

## Slice S1 — Complete sealed Combo evidence before deleting exports

### Owned behavior

Keep all runtime output behavior temporarily unchanged while making the sealed JSON sufficient to replace every
useful Combo candidate CSV.

### Slice contract

- Prerequisite: the current v1 snapshots/status and compatibility CSV paths are characterized by existing tests.
- Allowed scope: the listed snapshot, status, pipeline capture, Daily Brief/Advice, and focused test modules only.
- Non-goals: do not delete/rename any compatibility CSV or research/replay input in S1; do not change strategy policy.
- Completion signal: a manifest-bound v2 bundle is published/consumed for every owner combination while v1/CSV output
  remains behaviorally identical.
- Stop condition: any CSV proves to contain candidate facts that cannot be represented without a new strategy rule or
  materially broader schema; record the evidence and return to plan review.

### Implementation

1. Upgrade `src/application/combo_yield_candidate_snapshot.py` to v2 with strict normalization/validation for
   dependencies, scope statuses, Funding Put decisions, pair evaluations, rank shadow, and ranked pairs.
2. Replace the pair-only callback across `combo_yield_steps.py`, `symbol_monitoring.py`, `pipeline_symbol.py`, and
   `pipeline_watchlist.py` with one typed Combo evidence payload sink. Capture evidence before DataFrame attrs can be
   lost, partition SP+LC vs CC+LP by configured owner, and seal v2 once per account/run.
3. Upgrade CC+LP to a minimal v2 containing dependencies and normalized per-symbol scope statuses while preserving
   the v1 ranked-pair field set. Characterize the CSV before deletion; add candidate fields only if it proves the CSV
   contains facts absent from `ranked_pairs`.
4. Bind the same required-data/portfolio/ledger/FX/earnings-RV dependencies directly into each configured owner
   snapshot. Do not make Combo or CC+LP depend on an opening snapshot that may not be configured.
5. Additively publish `strategy_scan_status_index.v2.json` with exact owner/mode scope metadata and the immutable
   account-config hash. During S1/S2 only, continue the existing v1 index and candidate CSV publication for consumers
   not yet migrated; v2 and the manifest must not reference or hash those compatibility CSVs.
6. Seal each owner conditionally and independently: opening only when opening scopes exist, SP+LC only when SP+LC
   scopes exist, and CC+LP only when CC+LP scopes exist. Then publish `candidate_snapshot_manifest.v1.json` last from
   the validated v2 status index and exact owner snapshot hashes. If the v2 index has zero scopes, publish a valid
   empty terminal manifest with `completion_reason=no_applicable_scope`.
7. Add one manifest-first bundle loader and route Daily Brief, tick notification, and AI Advice candidate inputs
   through it. For new runs there is no per-owner fallback when the manifest is absent/invalid.
8. Add deterministic projections from a valid sealed snapshot for accepted candidates, rejected evaluations,
   pair-diagnostic summary inputs, and rank evidence. Put these beside the snapshot owner or in one narrow
   `candidate_snapshot_projection.py`; do not duplicate ranking/filter policy.

### Primary files

- `src/application/combo_yield_candidate_snapshot.py`
- `src/application/combo_yield_steps.py`
- `src/application/symbol_monitoring.py`
- `src/application/pipeline_symbol.py`
- `src/application/pipeline_watchlist.py`
- `src/application/candidate_snapshot_manifest.py` (narrow account-run terminal completeness owner)
- `src/application/strategy_scan_status.py` for v2 expected owner/mode scope projection
- `src/application/daily_decision_brief_service.py`, `src/application/tick_notification_flow.py`, and
  `src/application/ai_decision_advice/orchestration.py` for the manifest-first current-run consumer gate
- `src/application/cc_lp_candidate_snapshot.py` for the minimal v2 dependency/scope binding
- focused tests in `tests/test_combo_yield_candidate_snapshot.py`, `tests/test_combo_yield_steps.py`, and
  `tests/test_pipeline_capture_status_routing.py`

### Exit tests

- v2 round-trip, pandas/NumPy/null normalization, non-finite/unknown value rejection, tampered content/dependency,
  wrong run/account, malformed decisions/diagnostics, duplicate/unknown pair id, terminal-status mismatch,
  write-once conflict, zero-candidate, partial-data, and no-pair cases;
- frozen fixture parity between pre-seal DataFrames and v2 projections for final pairs, Funding Put decisions,
  diagnostic rejection counts/nearest misses, and rank shadow;
- one Put/multiple eligible Calls, one symbol/multiple eligible Puts, and cross-symbol cases prove eligible,
  ranked-below, and selected states remain distinct and consistent;
- multi-symbol/account partitioning proves evidence cannot cross account, variant, or run boundaries;
- SP+LC-only, CC+LP-only, opening-only, and mixed-owner runs prove independent sealing; interruption after any owner
  but before manifest publication remains incomplete for Daily Brief, Advice, research, and replay alike;
- manifest tests cover missing/extra owner, missing/extra scope, status-index tamper, snapshot tamper, wrong schema,
  write-once conflict, exact adoption, and a successful empty-owner `no_applicable_scope` commit;
- v2 status tests cover owner/mode mismatch, Combo variant mismatch, account-config hash mismatch, and prove no
  candidate CSV is a canonical v2 status artifact; characterization also proves the temporary v1/v2 dual publication
  is internally consistent;
- CC+LP v2 dependency/scope tamper tests and characterization prove its candidate CSV is redundant before later
  deletion.

## Slice S2 — Migrate all candidate evidence consumers

### Owned behavior

Make sealed run snapshots authoritative for automated evidence and replay, expose unsupported legacy runs explicitly,
and migrate the one candidate-like Shadow Replay intermediate to JSONL. Compatibility CSV and v1 status producers
remain temporarily active in this slice, but no migrated consumer may open their bytes.

### Slice contract

- Prerequisite: S1's v2 status, owner snapshots, terminal manifest, projections, and current-run bundle gate are
  accepted and committed.
- Allowed scope: the listed research/archive/shadow/Strategy Lab/CLI/docs modules and their focused tests.
- Non-goals: do not remove candidate CSV writers, v1 status publication, or `output_mode` in S2; do not infer legacy
  candidates from CSV.
- Completion signal: all automated consumers use manifest/snapshots/trace/JSONL, and CSV files can be made unreadable
  without changing supported-run results.
- Stop condition: a consumer requires a fact absent from the accepted v2 schemas or cannot expose unsupported history
  without silently changing dataset semantics; record it and return to plan review.

### Implementation

1. Refactor `research/evidence.py` to use the manifest-first bundle for opening, Combo v2, and CC+LP v2; derive
   candidate report, rejection, pair-diagnostic, and ranking summaries from JSON objects. Remove candidate/reject/
   pair-diagnostic CSV path inputs and parsers while keeping mark/outcome compatibility inputs unchanged.
2. Refactor `shadow_replay/capture.py`, `readiness.py`, and `candidate_impact.py` so sealed snapshots supply accepted
   and rejected candidate observations. Trace JSONL is supplementary only. Remove candidate/reject CSV selectors,
   globbing, parsers, and CLI flags.
3. Make dataset build/run-window coverage carry the six-state compatibility classification. New runs require the
   terminal manifest. For pre-manifest history only, validate the v1 status index plus the immutable account-run config,
   require its canonical hash to match consumed snapshots, and map each Combo scope to its configured variant before
   checking owner presence. Only then accept valid v1 snapshot facts as `supported_limited_legacy_snapshot`; this state
   remains non-authoritative for completeness/promotion. An unsupported scanned run cannot contribute inferred
   candidates and forces strict backtest/promotion false with a stable reason code.
4. Replace `combo_owned_underwritten_puts.csv` with a versioned JSONL projection of the manifest-bound Combo v2
   Funding Put calculation/underwriting decisions; do not rerun underwriting from a Sell Put candidate file. Bind the
   source receipt to the manifest and Combo snapshot hashes, and update `combo_evaluation.py` hash/schema/source
   validation atomically.
5. Change archive critical-file inventory and replay eligibility to sealed snapshot files plus trace. Legacy CSV names
   are retained only as non-parsing cold-archive classification metadata. Copying an untouched old run remains valid;
   building automated datasets from it is unsupported.
6. Update research CLI/profile payloads, Strategy Lab handoff, docs/runbooks/architecture text, and tests. Remove stale
   claims that diagnostics or ranked candidates are consumed from CSV.

### Primary files

- `src/application/research/evidence.py`, `src/application/research/service.py`, `src/application/research/archive.py`
- `src/application/shadow_replay/capture.py`, `readiness.py`, `candidate_impact.py`, `candidate_analysis.py`,
  `combo_funding.py`, `combo_evaluation.py`, and narrow shared helpers/constants as required
- `src/interfaces/cli/research.py`
- `docs/SHADOW_REPLAY_RUNBOOK.md`, `docs/STRATEGY_ARCHITECTURE.md`, `docs/AGENT_WIKI.md` where public contracts change
- focused research/archive/shadow/Strategy Lab/CLI tests and architecture guards

### Exit tests

- sealed SP/CC, Combo v2, CC+LP v2, manifest, mixed-account, zero-candidate, partial-data, tampered, and missing
  snapshot fixtures;
- CSV-only, trace-only, interrupted pre-manifest, mixed supported/limited/unsupported windows, and old Combo/CC+LP v1
  fixtures return the same explicit classification across research/archive/replay; limited or unsupported coverage
  cannot satisfy strict replay/promotion gates;
- no candidate CSV bytes are opened even while S1 compatibility producers still create them; a permission-denied/
  sentinel fixture proves metadata-only legacy filename detection;
- JSONL Combo Funding snapshot-projection/receipt/tamper/idempotency tests prove no candidate CSV is opened and no
  underwriting policy is recomputed;
- archive inventory/pull preserves old files, while dataset build skips or reports unsupported without parsing them;
- research rendered summaries and Strategy Lab readiness retain correct counts/reasons from sealed facts.

## Slice S3 — Contract candidate CSV production and compatibility surface

### Owned behavior

After S2 proves all consumers are snapshot-only, remove candidate compatibility CSV production, v1 status dual
publication, retired config/CLI surface, and dead file adapters. Required-data and unrelated approved CSV contracts
remain untouched.

### Slice contract

- Prerequisite: S2 is accepted and repository-wide reference evidence shows no automated consumer opens candidate CSV.
- Allowed scope: the listed candidate producer, manual renderer, status/config, dead-adapter, docs, and focused test
  modules; deletion is allowed only after reference proof.
- Non-goals: do not remove required-data, Close Advice, symbols summary, mark/outcome, or unrelated export CSVs; do not
  delete historical runtime files.
- Completion signal: every new enabled/disabled/empty/failure US/HK path produces zero forbidden candidate CSVs, all
  public retired flags/keys fail clearly, and the static guard passes.
- Stop condition: any remaining runtime consumer, unique CSV fact, calculation/ranking drift, or approved CSV false
  positive is found; preserve that path and return to the appropriate prior slice/review.

### Implementation

1. Make `candidate_scanning.py`, `scan_sell_put.py`, and `scan_sell_call.py` calculation-only: remove candidate
   `output`, reject-log output, empty CSV materialization, summary-path callbacks, and direct CSV CLI behavior.
   Required-data CSV remains the immutable calculation input.
2. Make `combo_yield_steps.py` fully in-memory: remove put-universe/labeled/cash/underwritten candidates, pair
   diagnostics, rank shadow, final candidate CSV, inline attachment, and empty CSV cleanup. Render optional manual
   alert text directly from typed rows.
3. Remove the CC+LP candidate writer from `cc_lp_steps.py`. Remove `out_path` candidate export behavior from
   `sell_put_cash.py` and the file adapter from `report_labels.py`, keeping pure DataFrame functions.
4. Remove unused CSV-only render/portfolio/reject-summary adapters after reference proof. Preserve
   `candidate_rule_label` and other pure functions still used by Agent/notification code.
5. Remove Combo `output_mode=inline|separate|both` defaults, resolver helpers, and runtime branching. Authored YAML use
   fails with an explicit removed-option error and new config builds omit it. Do not add a runtime compatibility
   loader: an old generated JSON is stale because the system-default source hash changes and must fail the existing
   identity/freshness preflight with its rebuild command. The controlled service-upgrade preparation already rebuilds
   US/HK runtime configs before code activation; add a fixture proving that rebuilt configs omit the key. The
   emergency `--allow-stale-config` may bypass freshness only, not the targeted removed-field validation.
6. Stop v1 strategy-status index dual publication and remove Combo candidate CSV from every canonical artifact/hash
   rule. Per-symbol status remains terminal JSON; the manifest-bound account-run snapshot bundle is the candidate seal.
7. Ensure trace rows refer to JSON/JSONL evidence identity, not a synthetic candidate CSV path. Delete remaining
   producer/manual-render candidate path parameters and CLI compatibility shims; research/replay input flags were
   already removed in S2.
8. Add a static production-source guard against the retired candidate filename/read/write patterns, allowing only the
   bounded legacy-filename classifier used for cold-archive status.

### Primary files

- `src/application/candidate_scanning.py`
- `src/application/scan_sell_put.py`
- `src/application/scan_sell_call.py`
- `src/application/combo_yield_steps.py`
- `src/application/cc_lp_steps.py`
- `src/application/sell_put_cash.py`
- `src/application/report_labels.py`
- `src/application/render_sell_put_alerts.py`, `render_sell_call_alerts.py`, and
  `render_yield_enhancement_alerts.py`
- `src/application/candidate_reject_summary.py` and `src/application/portfolio_capacity_shadow.py`
- `src/application/strategy_scan_status.py`
- `src/application/yield_enhancement_config.py`
- `src/application/config_defaults.py`, `src/application/config_validator.py`, `configs/system.json`
- `src/application/runtime_config_freshness.py` and existing service-upgrade config preparation tests for the explicit
  rebuild/fail-closed boundary; no retired-field runtime loader
- dead CSV-only adapters only after `rg` reference proof
- focused scan, Combo, config, status, symbol-monitoring, renderer, and forbidden-pattern tests

### Exit tests

- full enabled/disabled/empty/failure/success-empty US/HK symbol flows assert the forbidden candidate CSV set is
  empty while snapshots/statuses/traces remain valid;
- frozen calculations for SP, CC, SP+LC, and CC+LP match candidate rows, reasons, order, summaries, and alert text;
- strategy status validates without candidate CSV and detects tampered/missing status JSON; no v1 index is created;
- retired `output_mode` is absent from defaults/build output and rejected when authored; old generated runtime fails
  freshness with a rebuild command, controlled upgrade rebuild output omits it, and `--allow-stale-config` still fails
  removed-field validation rather than reviving output behavior;
- static source test rejects `read_csv`, `to_csv`, filename glob, or writer use tied to candidate compatibility names
  outside the explicit legacy-filename classifier.

## Cross-slice invariants and verification

### No behavior drift

Every serialization change is checked against in-memory or sealed-fixture facts. A difference in eligibility, reason,
rank, count, scope status, or notification conclusion is a defect unless separately approved by the user.

### No false authority

- missing/tampered/wrong-account/wrong-run snapshot: fail closed;
- valid sealed zero candidates: lawful `no_candidate` only when snapshot status says so;
- new run without a complete terminal candidate manifest: `unsupported_snapshot_missing`;
- new run with a terminal empty-owner manifest: `supported/no_applicable_scope`;
- pre-manifest valid v1 snapshot facts mapped through a hash-matching immutable account config:
  `supported_limited_legacy_snapshot` with strict completeness/promotion false;
- legacy candidate CSV without seal: `unsupported_legacy_csv_only`;
- trace without seal: `unsupported_snapshot_missing`;
- unsupported history never becomes an empty successful dataset.

### Quality gates

For each slice:

1. focused pytest for changed owners;
2. Ruff/compile or the project’s available static checks on changed Python;
3. dependency graph check when imports/modules change;
4. `git diff --check`;
5. DeepReview, fix, and re-review before the slice acceptance commit.

After all slices:

- candidate/snapshot/research/shadow/config/CLI focused suites;
- Agent plugin contract/smoke because tool payload/source contracts are adjacent;
- US/HK example config validate and build dry-run;
- full non-HTTP pytest baseline when resource limits permit;
- static forbidden-pattern audit scoped to production modules;
- aggregate DeepReview and PR DeepReview per Gateflow.

## Delivery and operations boundary

Gateflow may commit accepted gates, push the isolated branch, and open a draft PR. This work unit does not authorize
merging main, changing VERSION, publishing a release, deploying/upgrading a runtime, restarting services, modifying
production config/data, sending notifications, or deleting historical artifacts.

## Documentation decision

Update public scanner/research CLI references, Strategy Architecture, Shadow Replay runbook, and Agent Wiki wherever
the removed file paths, flags, status index, snapshot schemas, compatibility classification, or config key are part of
the documented contract. Do not rewrite unrelated strategy explanations. Each slice artifact records the exact docs
changed or an evidence-based no-doc-change decision.

## Completion report format

The final closeout records: accepted plan/slice/deepreview/PR-review commit hashes; files/contracts changed; focused
and broad validation with exact pass/fail counts; docs updated; every finding's final status; classified residual risks
and owner/destination; draft PR URL; confirmation that merge/release/deploy/history deletion were not performed; and
the next entry point after the user reviews the draft PR.

## Blocking questions

None. The user has approved the compatibility scope and historical-run policy. Any implementation discovery showing
that a supposedly redundant CSV contains unique facts pauses deletion of that file until the JSON owner is extended
and reviewed.
