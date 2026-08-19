---
name: om-pre-push-checks
description: Use before pushing or force-pushing an options-monitor branch, marking it ready for review, or claiming its outgoing changes are verified; determine the complete outgoing scope and run the smallest credible OM tests and guardrails without crossing into release, deployment, or production operations.
---

# OM Pre-Push Checks

Build evidence for the exact change leaving the worktree. This skill does not broaden authority: verification or review-readiness does not authorize staging, committing, pushing, merging, releasing, deploying, or production writes. Perform only the later actions the user explicitly requested.

## Establish the outgoing scope

1. Read the root `AGENTS.md` and the relevant ownership and verification entries in `docs/AGENT_WIKI.md`.
2. Confirm the repository root, current branch, worktree state, and intended base. Use the PR base, stack parent, or user-named base when known; do not silently assume `main` when the relationship is ambiguous.
3. Inspect all four parts of the prospective delivery: committed changes since the merge base, staged changes, unstaged changes, and untracked files.

```sh
git status --short --branch
git rev-parse --show-toplevel
git merge-base <verified-base> HEAD
git diff --name-status <merge-base>...HEAD
git diff --name-status --cached
git diff --name-status
git ls-files --others --exclude-standard
```

Keep unrelated user work out of the delivery. Never reset it, stage it for convenience, or treat an untracked file as irrelevant without inspecting its path and role.

## Select evidence

Trace each changed behavior through its owner and callers, then choose the narrowest test that would fail if that behavior regressed. Use the verification matrix in `docs/AGENT_WIKI.md`; do not duplicate it here.

- Run focused pytest files for the affected owner and its public CLI, Tool Gateway, persistence, or renderer facade.
- Run `./.venv/bin/python -m ruff check <touched-python-paths>` for changed Python. Use `.` only for genuinely cross-cutting changes or when a narrow scope is not credible.
- Run `git diff --check` for the committed outgoing diff and any staged or unstaged patch that is part of the delivery.
- Run the repository guardrails against staged content before commit or the tracked tree after commit:

```sh
./.venv/bin/python scripts/guardrails_check.py \
  --check-doc-wording \
  --check-runtime-config-tracking \
  --check-sensitive-artifacts
```

- For docs-only changes, verify referenced commands, paths, and public names against the current source when practical; wording checks alone do not prove behavior.
- For a shared contract, add the adjacent consumers it can affect. Run the full suite only when the change is truly repository-wide, focused evidence cannot be credible, or the user explicitly requests it.

Do not rerun a passing check merely because commit or push follows. Rerun it only when the tested code, test, configuration, generated input, or base changed.

## Stop conditions and handoff

Stop before an ordinary push when selected evidence fails. Fix only within the requested change; otherwise report the exact blocker. Do not bypass hooks or downgrade a check without explicit user direction.

For an explicitly authorized history rewrite, observe the current remote branch OID and use `--force-with-lease`; never use raw `--force`. After any authorized push, verify the remote branch resolves to local `HEAD` and report remote CI as passed, failed, or pending rather than assuming success.

Before handoff, report the verified base and scope, exact commands and outcomes, remaining gaps, and whether unrelated work, runtime artifacts, process artifacts, version files, release metadata, deployment changes, or production operations were excluded.
