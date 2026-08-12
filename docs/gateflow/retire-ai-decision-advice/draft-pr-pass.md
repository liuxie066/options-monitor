# Gateflow Draft PR Pass — Retire AI Decision Advice

## Gate

- Work unit: `retire-ai-decision-advice`
- Gate: `draft-PR-pass`
- Date: `2026-08-12`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/150`
- Base: `main@beb0836562c269e78a5f540659d20f76ee71e3d0`
- Accepted PR-review head: `3b0c2aa68bcd4c16f04219ce0287f6f5e33c3eef`
- State: `draft`, `OPEN`, `mergeable=true`, `mergeStateStatus=CLEAN`

## Entry Criteria

- [x] The accepted plan defines complete source retirement and explicitly excludes production cutover and historical cleanup.
- [x] Slice 1 removes Advice from current Brief normalization, rendering, Agent reads, diffs and outbound selection while preserving exact historical digest verification.
- [x] Slice 2 deletes generation, web-news collection, Advice-only portfolio preparation, config, managed service rendering and exclusive tests.
- [x] Candidate Engine, sealed candidate evidence, Close Advice, SQLite option positions, generic portfolio reads and Assistant LLM providers remain owned and tested.
- [x] Aggregate DeepReview findings were fixed and re-reviewed.
- [x] Candidate evidence-integrity PR #149 was integrated from latest `main`; its hard-evidence gap behavior remains present.
- [x] Latest-main and full PR reviews passed with no actionable finding.
- [x] The accepted PR review commit `3b0c2aa6` was pushed.
- [x] Latest-main focused integration suite passed: `485 passed`.
- [x] Sandbox-safe full suite passed: `4459 passed, 10 skipped, 5 existing warnings`.
- [x] Localhost-only HTTP quality suite passed: `4 passed`.
- [x] Ruff, compileall, dependency graph and `git diff --check` passed.
- [x] US/HK example config validate/build dry-runs passed.
- [x] GitHub Agent Plugin, Guardrails and all CodeQL checks passed on `3b0c2aa6`.
- [x] The PR body reflects current validation, retained authorities and the separate production-cutover boundary.
- [x] Residual risks have explicit destinations and do not require hidden compatibility code.
- [x] This is not an issue-driven work unit; no issue closing keyword or issue mutation is required.

## Finding Status

- Plan review: accepted findings fixed; re-review passed.
- Slice 1: accepted findings fixed; re-review passed.
- Slice 2: accepted finding fixed; re-review passed.
- Aggregate: accepted findings fixed; re-review passed.
- Latest-main propagation: passed with no actionable finding.
- Full PR review: passed with no actionable finding.
- Open or unclassified findings in this work unit: none.

## Residual Risks / Owners

- Deployed `ai_decision_advice` config and installed Collector units: separately authorized production reconcile.
- Historical Advice/evidence artifacts and their disk use: separately authorized, recoverable data-cleanup work unit.
- Unknown external private importers of deleted internals: external consumer owner; no compatibility shim by design.

These are explicit post-source boundaries and do not block this Draft PR.

## Safety Boundary

No merge, Ready transition, reviewer request, approval, release, tag, deployment, remote upgrade, service/config/secret
mutation, runtime data write, notification send or historical deletion was performed. The original dirty worktree and its
protected stash were not staged, reset, restored or altered.

## Conclusion

`draft-PR-pass`.

Current gate / next entry point: `final closeout`. The PR remains Draft.
