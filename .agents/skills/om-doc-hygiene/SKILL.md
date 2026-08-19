---
name: om-doc-hygiene
description: Use when writing, reviewing, or trimming options-monitor documentation, comments, prompts, diagnostics, or user-visible text; preserve OM facts and safety contracts, remove authoring-session residue, and update the current owner instead of duplicating history or generated prose.
---

# OM Documentation Hygiene

Keep prose useful at the current commit: enough to preserve behavior and safety, without design-session narration, duplicated ownership, or invented financial facts. Use the requested files or diff as the scope; do not turn a local edit into a repository-wide cleanup.

Review and audit requests are read-only. Edit only when the user asks to write, fix, trim, or update prose.

## Find the owner

Read the applicable `AGENTS.md`, `docs/INDEX.md`, and the source, test, config validator, or runtime contract that owns the statement. Resolve conflicts in the authority order documented by `docs/INDEX.md`.

- Put stable agent guidance in `AGENTS.md`, detailed task guidance in `docs/AGENT_WIKI.md`, current architecture in its indexed living document, and public tool schemas in the runtime/tool owner.
- Update one canonical owner and link to it. Do not create another design document when an indexed owner already exists.
- Treat generated catalogs, examples, snapshots, and fixtures as derivative. Edit their source and regenerate them when the workflow requires it.
- Treat `docs/reviews/`, `docs/plans/`, and `docs/gateflow/` as historical process artifacts. Create them only for the workflow that requests them, never modernize them into living documentation, and never force-add them.

## Preserve the proposition

Before trimming, preserve every relevant actor, action, condition, ordering rule, must/may/never distinction, authority, side effect, failure state, and consequence.

- Do not let wording change counts, thresholds, strategy conclusions, reason codes, statuses, command behavior, account scope, or whether an action writes durable state.
- Keep missing or unavailable data explicit. Do not turn inference into fact or a provider/model failure into a valid zero-result outcome.
- Preserve preview, confirmation, idempotency, receipt, readback, notification, broker, and production boundaries exactly.
- Comments explain non-obvious rationale, invariants, ownership, or failure behavior; delete narration that merely restates control flow.
- User-facing Chinese copy should explain the project and function before strategy detail, remain restrained, and preserve advisory and risk boundaries.

## Remove authoring residue

Rewrite prose from the repository's current-state vantage when it depends on a vanished session, draft, or reviewer. Typical residue includes unresolved decision or audit labels, draft section references, “this PR/commit,” reviewer-addressed defenses, change narration on current-state pages, walkthroughs of obvious code, and hedged plans without an owned issue or `TODO`.

Do not erase legitimate history. Changelogs, migration notes, postmortems, and requested review artifacts may describe past changes when that is their purpose. Keep resolvable issue references, measured bounds, suppression reasons, and counterfactual explanations that prevent a regression.

## Verify

Re-read the final passage without relying on the authoring conversation. Confirm every internal reference resolves at the current commit and every shown command, path, tool, status, and field exists or is clearly marked as proposed.

Run `git diff --check`. For tracked documentation, also run the relevant repository wording and sensitive-artifact guardrails. Treat prompts, diagnostics, CLI text, and rendered notification wording as behavior: run the smallest owning snapshot or facade test instead of claiming a prose-only change.

Report the inspected scope, edits made, deliberate keeps, unresolved factual gaps, and checks actually run.
