---
name: om-doc-hygiene
description: Check OM documentation and user-facing text against the correct source of truth, preserving strategy, broker, ledger, and production safety contracts.
---

# OM documentation hygiene

Use the general `repo-doc-hygiene` workflow when available. This skill adds OM-specific evidence and safety rules. An audit is read-only; edit only within the requested scope.

Read the applicable `AGENTS.md`. For factual or structural edits, use `docs/INDEX.md` to find the living document and select evidence by question type. Current broker facts come from OpenD, effective runtime facts from the target host and config, and local position facts from the ledger. Preserve conflicts and unknown data; do not turn missing data into zero or inference into fact.

Keep stable agent guidance in `AGENTS.md`, detailed procedures in `docs/AGENT_WIKI.md`, and tool contracts with their runtime owners. Edit generated material through its source. `docs/reviews/`, `docs/plans/`, and `docs/gateflow/` are process artifacts; do not turn them into living docs or force-add them.

Wording must preserve amounts, quantities, thresholds, account and market scope, strategy conclusions, reason codes, statuses, write effects, and preview/confirmation/readback boundaries. Comments should explain non-obvious rationale or failure behavior. User-facing Chinese copy should remain clear, restrained, and explicit about advisory or risk limits.

After edits, check references and `git diff --check`, then run the repository's applicable wording and sensitive-artifact guardrails. Runtime prompts, diagnostics, CLI text, and notifications are behavior: run the smallest owning facade or snapshot check. Report unresolved factual gaps.
