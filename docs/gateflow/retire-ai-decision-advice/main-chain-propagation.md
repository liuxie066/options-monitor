# Gateflow Artifact — Main-chain Propagation

- Work unit: `retire-ai-decision-advice`
- Gate: `ready-to-open-draft-PR` prerequisite
- Branch: `refactor/retire-ai-decision-advice`
- Base: `origin/main@beb0836562c269e78a5f540659d20f76ee71e3d0`
- Review artifact: `docs/reviews/code-review-20260812-234102.md`
- Decision: `pass`
- Artifact path: `docs/gateflow/retire-ai-decision-advice/main-chain-propagation.md`

## Propagation result

The five accepted Gateflow commits were rebased onto the latest remote main, including merged candidate
evidence-integrity PR #149. Conflict resolution preserved the sealed-candidate integrity checks and the specific
hard-evidence-gap reminder while maintaining the retirement invariant: AI Advice does not affect candidate selection,
rendering, Agent reads, or delivery.

The retirement design path remains a tombstone instead of restoring the former live design. `CHANGELOG.md` keeps the
released 1.13.13 entry and adds the retirement under Unreleased. Generated dependency documents were regenerated from
the final tree.

## Validation

```text
latest-main focused integration suite: 485 passed
full sandbox-safe suite: 4459 passed, 10 skipped, 5 warnings
localhost-only HTTP suite: 4 passed
ruff, compileall, dependency graph check: passed
US/HK example config validate/build dry-runs: passed
latest-main DeepReview: pass; no actionable findings
```

## Protected worktree evidence

- Original repository remains on `main@b85607be`; it was not advanced or rewritten while its unrelated work is dirty.
- The separately preserved stash `codex-preserve-before-main-us-notification-20260812` remains present.
- Its exact binary patch hash is `9ba116779673b0d5485d4ea5cc29cee0950e9b72c972627b395a23cb435de87f`.
- Unrelated ledger, Futu gateway and settlement-test edits visible in the original worktree were not staged, modified,
  or transferred into this branch.

## Residual risks

- Production config/service reconciliation and historical-data cleanup require separate explicit authority.
- External private importers of deleted internal modules remain outside repository visibility.

## Completion status and next gate

Latest-main propagation passed. Next entry point: update Draft PR #150 with `--force-with-lease`, then run the PR review
and checks gates.
