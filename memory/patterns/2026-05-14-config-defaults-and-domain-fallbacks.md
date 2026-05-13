## Context

Some runtime defaults are represented both in `configs/system.json` and as
domain-level fallback values for tests or no-config callers.

## Pattern

- When changing a system default, check for matching domain fallback constants.
- Update docs/examples that present the default as operator guidance.
- Run both targeted domain tests and layered config dry-runs to cover the two
  paths.
