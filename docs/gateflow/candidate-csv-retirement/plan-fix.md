# Gateflow Fix Artifact — Candidate Compatibility CSV Retirement Plan

- Gate: `fix`
- Work unit: `candidate-csv-retirement`
- Scope: accepted findings from three failed PlanReview passes
- Changed files: `docs/gateflow/candidate-csv-retirement/plan.md` and review artifact metadata
- Docs decision: plan/review artifacts only; product/runbook docs belong to approved implementation slices
- Completion status: fixes accepted by PlanReview re-review 3 (`pass-with-risks`)
- Artifact path: `docs/gateflow/candidate-csv-retirement/plan-fix.md`

## Finding decisions and final fix state

| Finding | Decision | Fix state | Resolution |
|---|---|---|---|
| PR-01 | accepted | 已修复 | Added a write-once terminal candidate manifest and a v2 expected-scope index. |
| PR-02 | accepted | 已修复 | Split Combo pair evidence into eligible, ranked-below, and selected states with reference validation. |
| PR-03 | accepted | 已修复 | Made opening, SP+LC, and CC+LP owner seals conditional and dependency-independent. |
| PR-04 | accepted | 已修复 | Added `supported_limited_legacy_snapshot` with strict promotion disabled. |
| PR-05 | accepted | 已修复 | Selected controlled config rebuild/freshness failure; no runtime compatibility loader. |
| PRR1-01 | accepted | 已修复 | Made the manifest-first bundle the mandatory current-run gate for Daily Brief, tick notification, and AI Advice. |
| PRR1-02 | accepted | 已修复 | Added owner/mode/config binding to status index v2 and immutable-config mapping for legacy v1. |
| PRR1-03 | accepted | 已修复 | Defined empty terminal manifest as `supported/no_applicable_scope`. |
| PRR1-04 | accepted | 已修复 | Named the existing freshness/service-upgrade rebuild boundary and covered all stale-config behavior explicitly. |
| PRR2-01 | accepted | 已修复 | Reordered slices to expand (dual publish), migrate consumers, then contract producers. |

No finding was rejected or deferred.

## Additional consistency fixes

- Upgraded CC+LP to a minimal v2 because v1 cannot satisfy independent dependency/scope binding.
- Added deterministic compatibility-classification precedence.
- Bound the Shadow Replay Funding Put JSONL projection directly to Combo v2 rather than recomputing from candidate CSV.
- Named all directly observed candidate CSV adapters/consumers, including candidate analysis/evaluation, reject summary,
  portfolio-capacity shadow, and manual renderers.
- Added per-slice prerequisites, allowed scope, non-goals, completion signals, stop conditions, documentation decision,
  and final closeout format required by Gateflow.

## Validation

- Re-read the status, pipeline, snapshot, Daily Brief/Advice, runtime-config, research, archive, and Shadow Replay source
  boundaries cited by the reviews.
- Ran repository-wide candidate filename/read/write reference inventories to check plan ownership.
- `git diff --check`: pass after the plan fixes.
- Implementation tests are intentionally deferred to the corresponding approved slices; no production code changed in
  the plan gate.

## Residual risks and ownership

- Combo v2 payload size: covered by approved S1 characterization and recorded byte/row-count evidence.
- Pre-manifest v1 status mutability: covered by S2's explicit limited/non-promotion classification; it will not be
  upgraded into modern authority.
- Hidden candidate CSV references: covered by S2 sentinel tests and S3 repository static guard before producer removal.
- Runtime config activation: source behavior is covered by S3 freshness/service-upgrade tests; an actual release or
  runtime upgrade is assigned to a separately authorized future operations work unit.
- Full-suite resource limits: covered by aggregate verification; any skipped/failed area must remain a classified
  residual risk and cannot be silently closed.
