# Archived Project Memory

This directory is archived reference material for `options-monitor`.

Its purpose is to preserve older durable engineering context: decisions that affected architecture, patterns that were useful, and failures that should not be repeated. It is not an active LLM wiki, not a source of current truth, and not a workflow for adding new memory.

## Authority Order

Use this order when records disagree:

```text
Source code / tests / runtime evidence
> AGENTS.md / docs/ARCHITECTURE.md
> memory/decisions
> memory/patterns / memory/failures
```

Notes:

- `docs/ARCHITECTURE.md` is the current architecture authority.
- `docs/AGENT_WIKI.md` is the agent operating manual and code ownership map.
- `memory/index.md` is the navigation entry for old records.
- `memory/decisions` contains archived design decisions.
- `memory/patterns` contains archived reusable implementation patterns.
- `memory/failures` contains archived failure lessons.

## Use Policy

- Read memory only when the task needs historical context or prior decisions.
- Open only the relevant entries from `memory/index.md`.
- Treat every memory fact as stale until verified against current code, tests, config, docs, or runtime artifacts.
- Do not use old memory entries as justification for changing live config, Feishu, position state, trade events, notifications, services, or broker-facing data.

## No Ingest Workflow

This repo no longer maintains a manual memory-ingest workflow.

- Do not treat prompts like "ingest this change" or "memory lint" as a standing project command.
- Do not routinely add entries, update `memory/index.md`, or append to `memory/log.md` during normal work.
- Prefer updating current docs, tests, or runtime read surfaces when a behavior or boundary changes.
- If a future task explicitly asks to preserve historical context, handle it as a one-off documentation change with current evidence and user approval.

Old entry templates were archived under `memory/_archive/templates/` for reference only.
