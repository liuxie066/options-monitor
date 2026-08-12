# Gateflow Fix Artifact — Aggregate DeepReview

- Work unit: `retire-ai-decision-advice`
- Gate: aggregate `fix`
- Review artifact: `docs/reviews/code-review-20260812-223939.md`
- Status: fixes complete; pending aggregate re-review
- Artifact path: `docs/gateflow/retire-ai-decision-advice/aggregate-review-fix.md`

## Finding decisions

### DR-AGG-01 — accepted

Remove `### AI建议` from the shared mobile Markdown allowlist and replace the stale former-design-section comment with
the current deterministic candidate-heading contract.

Final status: `已修复`. An explicit negative regression now proves the retired heading fails the shared contract.

### DR-AGG-02 — accepted

Add an exact read-only service drift regression that presents the retired Collector service/timer in both the persisted
profile and a temporary systemd unit root, then proves both are reported as extra without confirm/apply.

Final status: `已修复`. The regression also proves no operation is emitted and both temporary unit files remain intact.

## Validation plan

- Notification formatting and Daily Brief renderer suites.
- Exact service drift regression plus service-deploy suite selection.
- Full suite split into sandbox-safe aggregate run and the one localhost-only HTTP test.
- Ruff, compileall, dependency graph, config dry-runs, diff checks, and protected original-worktree hash.

## Docs decision

The current retirement/deployment docs already state the correct operational boundary. The fix supplies the missing
executable evidence and removes a stale test comment; no product-doc expansion is necessary.

## Residual risks and uncovered areas

- **covered by current fix loop**: both accepted aggregate findings.
- **requiring new issue or explicit user decision**: production cutover and external private importers remain outside
  source-only scope.

## Validation

```text
targeted aggregate fixes: 78 passed, 4 existing deprecation warnings
exact negative-heading, Collector-drift and dependency generator tests: 6 passed
sandbox full suite excluding localhost-only quality HTTP file: 4441 passed, 10 skipped, 5 warnings
localhost-only quality HTTP file: 4 passed
one unrelated lifecycle lease timing test failed once in an earlier aggregate run and passed on immediate isolated
retry; the final full run passed it
ruff check .: passed
compileall: passed
dependency graph check: current; production_modules=568; cycles=0
git diff --check: passed
```
