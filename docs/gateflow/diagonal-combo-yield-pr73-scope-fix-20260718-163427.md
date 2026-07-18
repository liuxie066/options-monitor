# Gateflow PR Scope Fix

- Gate: PR post-push review -> fix -> re-review
- Work unit: true staggered/diagonal Combo Yield lifecycle
- PR: `#73`
- Finding: `PR-F2`
- Review artifact: `docs/reviews/pr-73-review-20260718-163427.md`
- Artifact path: `docs/gateflow/diagonal-combo-yield-pr73-scope-fix-20260718-163427.md`

## Fix

Removed `docs/gateflow/combo-yield-sell-put-runtime-decoupling-goal-confirmation-20260718.md` from PR tracking without deleting the local working copy. The document is a separate, awaiting-confirmation Gateflow work unit and is preserved as untracked state.

## Validation

- The path is absent from both `origin/main` and pre-merge head `ad6f4627`.
- After the staged deletion, `git diff origin/main --name-status` no longer includes it.
- No production code changed in this fix.

## Residual Risk

- Future broad staging must continue to exclude the preserved untracked artifact.

## Completion Status

- `PR-F2`: fixed; ready for docs-only accepted scope-fix commit and push.
