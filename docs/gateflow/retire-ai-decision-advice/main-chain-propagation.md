# Gateflow Artifact — Main-chain Propagation

- Work unit: `retire-ai-decision-advice`
- Gate: `ready-to-open-draft-PR` prerequisite
- Branch: `refactor/retire-ai-decision-advice`
- Base: `main@b85607be9872ba8b3cc7d21db82dafeda416dce9`
- Review artifact: `docs/reviews/code-review-20260812-230359.md`
- Decision: `pass`
- Artifact path: `docs/gateflow/retire-ai-decision-advice/main-chain-propagation.md`

## Propagation result

The four accepted Gateflow commits were rebased onto current `main`. Conflict resolution kept the v1.13.13 compact
Daily Brief presentation and confirmed-source reminder behavior, while preserving the retirement invariant: AI Advice
does not affect candidate selection, rendering, Agent reads, or delivery.

The retirement design path remains a tombstone instead of restoring the former live design. `CHANGELOG.md` keeps the
released 1.13.13 entry and adds the retirement under Unreleased. Generated dependency documents were regenerated from
the final tree.

## Validation

```text
focused retirement suite: 526 passed
full sandbox-safe suite: 4445 passed, 10 skipped, 5 warnings
localhost-only HTTP suite: 4 passed
ruff, compileall, dependency graph check: passed
US/HK example config validate/build dry-runs: passed
post-rebase DeepReview: pass; no actionable findings
```

## Protected worktree evidence

- Original repository remains on `main@b85607be`.
- The separately preserved stash `codex-preserve-before-main-us-notification-20260812` remains present.
- Its exact binary patch hash is `9ba116779673b0d5485d4ea5cc29cee0950e9b72c972627b395a23cb435de87f`.
- Unrelated ledger edits visible in the original worktree were not staged, modified, or transferred into this branch.

## Residual risks

- Production config/service reconciliation and historical-data cleanup require separate explicit authority.
- External private importers of deleted internal modules remain outside repository visibility.

## Completion status and next gate

Main-chain propagation passed. Next entry point: `ready-to-open-draft-PR -> push`.
