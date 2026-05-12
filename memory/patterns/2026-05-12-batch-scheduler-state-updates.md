## Context

Multi-account tick needs account-scoped scheduler decisions and state updates,
but spawning one scheduler CLI process per account adds avoidable overhead to the
cron path.

## Pattern

- Keep the global scheduler CLI call for the run-level audit surface.
- Build per-account scheduler decisions in-process from the same config, state,
  and domain decision function.
- Batch account scheduler state writes in-process after successful scans or
  confirmed sends.
- Preserve old no-op behavior for empty account lists.
