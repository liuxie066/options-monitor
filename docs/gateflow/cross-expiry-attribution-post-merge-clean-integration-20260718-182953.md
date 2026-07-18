# Gateflow Post-Merge Clean Integration — Cross-Expiry Attribution

## Gate

- Work unit: cross-expiry yield and capital attribution aggregate deepreview follow-up
- Gate: clean-branch reconciliation and integration
- Branch: `codex/cross-expiry-attribution-post-merge-deepreview`
- Cross-expiry merge target reviewed: `origin/main@6dd76433`
- Latest integration base observed before commit: `origin/main@e128b412` (`v1.2.412`)
- Historical clean candidate preserved: `codex/cross-expiry-yield-capital-attribution-clean-final@a2e6e148`
- Status: pass
- Artifact path: `docs/gateflow/cross-expiry-attribution-post-merge-clean-integration-20260718-182953.md`

## Repository Reconciliation

PR #78 was merged externally while the independent clean candidate was under aggregate review. Its remote head branch was then deleted. Rewriting or retargeting PR #78 was therefore no longer a legal or useful integration action.

The previous squash candidate remains preserved by its local branch. This follow-up branch starts from merged `main` and contains only aggregate-review fixes, regression tests, generated dependency evidence, and Gateflow/review artifacts. It does not duplicate the already-merged cross-expiry implementation.

A later release-only `main` delta (`CHANGELOG.md` and `VERSION`, PR #79 / tag `v1.2.412`) will be integrated before push; no attribution production overlap exists.

## Intended Diff

- `domain/domain/performance/strategy_attribution.py`
  - residual-tail quality follows actual gross/net PnL evidence after time isolation.
- `domain/domain/performance/engine.py`
  - assigned-stock metadata conflict/incomplete handling follows snapshot-first/top-level-fallback and fail-closed semantics.
- `tests/test_performance_strategy_attribution.py`
  - isolated missing-mark, assigned-stock conflict, missing-group, and ordinary non-Combo regressions.
- `docs/DEPENDENCY_GRAPH.md`
  - regenerated test import counts only; production module/edge topology remains unchanged.
- Review/Gateflow artifacts documenting findings, decisions, fixes, validation, and residual-risk classification.

## Safety

- No production config mutation.
- No notification delivery.
- No ledger, option-position, assigned-stock, trade-event, or broker-facing write.
- Worktree-only `.venv` symlink was removed after tests.
- No branch deletion, merge, ready-for-review transition, approval, reviewer request, or external comment was performed.

## Residual Risks

| Risk | Classification / owner |
|---|---|
| Exact intra-period Call split across Put close still requires a transition mark | assigned to later evidence-capture work unit |
| One long Call funding multiple sequential Put cycles | assigned to future multi-cycle attribution work unit |
| Historical rows without canonical strategy provenance | assigned to data-repair/backfill work unit |
| Broker-margin/NAV return methodology | assigned to portfolio capital methodology work unit |

No unclassified residual risk remains.
