# Memory Workflow

This directory is the project-level LLM wiki for `options-monitor`.

Its purpose is to preserve durable engineering knowledge: decisions that affect future architecture, patterns worth reusing, failures that should not be repeated, and short session handoffs. It is not a replacement for source code, tests, runtime evidence, or architecture docs.

## Authority Order

Use this order when records disagree:

```text
Source code / tests / runtime evidence
> AGENTS.md / docs/ARCHITECTURE.md
> memory/decisions
> memory/patterns / memory/failures
> memory/sessions
```

Notes:

- `docs/ARCHITECTURE.md` is the current architecture authority.
- `docs/AGENT_WIKI.md` is the agent operating manual and code ownership map.
- `memory/decisions` records durable design choices and their validation.
- `memory/patterns` records reusable project-specific implementation patterns.
- `memory/failures` records mistakes, traps, and the safer future behavior.
- `memory/sessions` is short-term handoff context and must not become the long-term architecture source of truth.

## When To Ingest

Default behavior: do not automatically ingest.

Do not update memory for every edit, every debugging step, or every session summary. Ingest only when the result is likely to improve future judgment.

Good ingest triggers:

- A new architecture decision or ownership boundary.
- A boundary change in a module owner, public facade, CLI, tool payload, output path, safety rule, or runtime behavior.
- A failure mode was diagnosed and is likely to recur.
- A reusable implementation pattern was proven by code and tests.
- A release or verification cycle produced a stable operational lesson.

Do not ingest:

- Temporary debug notes.
- Raw command output without a durable lesson.
- Guesses that were not checked against code, tests, config, or runtime artifacts.
- Long summaries that duplicate existing docs.
- Session chronology that does not change future decisions.

Before writing an entry, answer:

1. Will this change how a future agent designs, debugs, verifies, or releases work?
2. Is the lesson backed by code, tests, config, runtime evidence, or a completed release?
3. Does it belong in long-term memory instead of only `memory/sessions`?
4. Would adding this entry reduce confusion more than it adds maintenance cost?

If any answer is unclear, keep the information in the session summary and do not promote it yet.

## Manual Triggers

Use natural-language prompts first. Do not add automated writes until this workflow is proven.

Common prompts:

```text
ingest 这次改动
更新本次 session memory
请根据本次 session 更新 memory
memory lint
ingest this change into memory
update this session memory
please update memory from this session
```

An ingest pass should:

1. Read the relevant diff, test results, runtime evidence, and session summary.
2. Decide whether the lesson belongs in `decisions`, `patterns`, `failures`, or only `sessions`.
3. Write the smallest useful entry with evidence and validation.
4. Update `memory/index.md` when the new entry should be discoverable by module.
5. Append an audit entry to `memory/log.md`.

## File Naming

Use:

```text
memory/<section>/YYYY-MM-DD-short-kebab-title.md
```

Examples:

```text
memory/decisions/2026-05-20-agent-memory-authority-order.md
memory/patterns/2026-05-20-read-only-before-runtime-mutation.md
memory/failures/2026-05-20-session-summary-treated-as-architecture.md
```

## Lint Expectations

A memory lint pass should check:

- The entry has a clear owner section: decision, pattern, or lesson.
- The entry points to validation or evidence.
- It does not contradict `docs/ARCHITECTURE.md`, `docs/AGENT_WIKI.md`, or active tests.
- It does not duplicate an existing entry without explaining replacement or refinement.
- Long-lived conclusions are indexed in `memory/index.md`.
