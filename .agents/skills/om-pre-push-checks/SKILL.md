---
name: om-pre-push-checks
description: Apply OM-specific tests and guardrails to the exact outgoing change before push or review-readiness; preserve unrelated work and delivery boundaries.
---

# OM pre-push checks

Use the general `pre-push-checks` workflow when available to establish base, committed diff, index, working-tree edits, and untracked files. This skill supplies OM's checks. Read `AGENTS.md` and the relevant ownership and verification entries in `docs/AGENT_WIKI.md`; do not include unrelated user work in the outgoing change.

Choose the smallest tests that would fail for the changed behavior. Include the public CLI, Tool Gateway, persistence, or renderer when its contract changes, plus affected consumers of shared contracts. Run Ruff for changed Python paths; use repository-wide Ruff or pytest only when the scope or a required gate calls for it. Verify documentation references against their owners and run `git diff --check` for the outgoing patch.

Before commit, check the exact staged index, including partially staged files:

```sh
./.venv/bin/python scripts/guardrails_check.py --staged \
  --check-doc-wording \
  --check-runtime-config-tracking \
  --check-sensitive-artifacts
```

After commit, run the same guardrail command without `--staged` only when the tracked tree matches the outgoing commit; otherwise validate the exact commit separately. Reuse passing checks only while their inputs remain unchanged. Preserve mandatory CI checks and report any failed or missing evidence. Verification does not authorize a later delivery stage or production action.
