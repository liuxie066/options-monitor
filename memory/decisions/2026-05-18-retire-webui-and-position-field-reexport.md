## Context

The project is no longer preserving the old local WebUI during the position/trade core model rebuild. Keeping WebUI-specific config patchers, presenters, frontend assets, and stale project memory would keep a second configuration surface alive while the active runtime is CLI/agent centered.

At the same time, runtime code still had imports from the legacy `domain.domain.option_position_lots` re-export even though the canonical owner is `domain.domain.ledger.position_fields`.

## Decision

1. Retire the old local WebUI surface completely instead of compatibility-preserving it.
2. Keep first-time setup and operations on `./om init runtime`, `./om`, and `./om-agent`.
3. Point runtime code at `domain.domain.ledger.position_fields` directly.
4. Remove retired v2 projection status payloads from option-position workflow outputs.
5. Keep structural tests that prevent WebUI entrypoints and legacy position field imports from returning.

## Verification

- `python3 -m pytest -q`: 1320 passed.
- Focused retirement tests: 89 passed.
- Final structural tests: 15 passed.
- `python3 -m compileall ...`: passed.
- `python3 -m basedpyright --level error src/application/agent_tool_init_local.py`: 0 errors.
- `git diff --check`: passed.
