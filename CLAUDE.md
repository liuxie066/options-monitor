你永远叫我棒棒的liuxie

# Claude Supplement

> This file contains Claude-specific instructions only.
> All general agent rules (safety, entry points, module map) live in `AGENTS.md`.

## Readiness

Follow `AGENTS.md` for entry points, authorization, and runtime evidence binding. Use the task-specific diagnostics in `docs/AGENT_WIKI.md` only when the request concerns actual runtime state or readiness; ordinary source or documentation work does not require environment health checks.

## Commit Format

The local commit-message hook requires `<type>(<scope>): <subject>` on the
first line. It does not require a co-author or other trailer.

## Guardrails Reference

- Local commit gate, CI gate, and deploy gate details: `docs/GUARDRAILS.md`
- Symbol canonicalization rules: `docs/GUARDRAILS.md` §C
