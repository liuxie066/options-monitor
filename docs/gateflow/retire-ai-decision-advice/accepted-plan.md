# Gateflow Accepted Plan — Retire AI Decision Advice

- Work unit: `retire-ai-decision-advice`
- Gate: `plan -> plan review -> fix -> re-review`
- Decision: `pass-with-risks`
- Plan: `docs/gateflow/retire-ai-decision-advice/plan.md`
- Initial review: `docs/reviews/plan-review-20260812-112642.md`
- Fix artifact: `docs/gateflow/retire-ai-decision-advice/plan-review-fix.md`
- Accepted re-review: `docs/reviews/plan-review-20260812-113353.md`
- Artifact path: `docs/gateflow/retire-ai-decision-advice/accepted-plan.md`

## Accepted findings

- Slice 1 retains ignored private handoff arguments until Slice 2 atomically removes every caller/callee field.
- The current Brief normalizer strips retired keys; exact raw values exist only inside overlay-era digest candidates.
- Repository owns frozen-envelope raw source/text/card classification before any outbound map write.
- Root YAML, market YAML, and runtime JSON all produce targeted retirement errors for the exact key.

## Approved implementation boundary

1. Slice 1 cuts Advice from current Brief normalization, assembly, render/read output, material diffs, and frozen retry
   selection while preserving immutable historical digest integrity.
2. Slice 2 deletes the generation/collector/config/service/preparation subsystem and its exclusive code/tests, while
   preserving Candidate Engine, Close Advice, option-position authority, generic portfolio queries, and shared LLM
   providers.
3. No production config/service/data/notification mutation, merge, release, deployment, or historical cleanup.

## Validation decision

Each slice requires focused tests, compileall, and diff checks. Aggregate requires all focused suites, full pytest,
example-config validate/build dry runs, dependency validation/regeneration, active-reference search, and protected
dirty-worktree hash verification.

## Documentation decision

Slice 2 updates current operator docs, rewrites the existing AI design path as a retirement record, appends CHANGELOG,
and regenerates dependency documentation. Historical release/review artifacts remain unchanged.

## Residual risks

- Installed collector units — `assigned to later work unit` with explicit production-service authorization.
- Historical AI artifacts and disk usage — `assigned to later work unit` with destructive-data authorization.
- Unknown third-party internal-Python importers — `assigned to later work unit`; current public contracts and repository
  callers are covered.
- Already accepted provider deliveries — `assigned to later work unit`; retraction is outside scope.

## Completion status and next gate

Plan gate passed. Next entry point: Slice 1 implementation, followed by focused validation and DeepReview.
