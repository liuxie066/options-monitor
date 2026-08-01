# Implementation Evidence

## Implemented Scope

- Strategy Lab CLI now resolves OpenD fetch limits from its service-profile market configs and combines them conservatively.
- The resolved runtime root and fetch configuration flow through Strategy Lab update, Shadow Replay data-plan, and mark collection into the canonical OpenD fetch request.
- Persistent sampling uses the runtime-owned OpenD cache/limiter base; dry-run sampling continues to substitute temporary roots.
- Generated bounded systemd one-shots now render `TimeoutStartSec` instead of ignored `RuntimeMaxSec`.
- Operator documentation and the generated dependency graph are current.

## Focused Validation

- `233 passed` across:
  - `tests/test_research.py`
  - `tests/test_strategy_lab.py`
  - `tests/test_shadow_replay.py`
  - `tests/test_service_deploy.py`
- Ruff changed-file check: pass with `--no-cache`.
- Dependency graph regeneration/check: pass; 576 production modules, zero cycles.
- `git diff --check`: pass.
- Full isolated suite: `3902 passed, 10 skipped, 6 warnings` in 60.03 seconds.
  - The suite ran from a clean detached `/private/tmp` worktree with the exact tracked diff applied and the repository virtualenv linked read-only.
  - This avoids both sandbox denial on legacy repository-output tests and contamination from the current workspace's shared OpenD limiter state.

## Safety Notes

- No production command, notification, broker write, trade write, final Position Advice CAS, or runtime deployment was performed during implementation.
- The first provider rate-limit circuit-breaker merged in PR #127 remains unchanged.
