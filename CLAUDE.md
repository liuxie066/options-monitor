# Claude / OpenClaw Instructions

This repository is an operations-sensitive options monitoring tool. Treat it as a controlled operational system, not a generic Python sandbox.

## Default Behavior

- For requests such as "explain", "look into", "check", "why", or "how does this work", start in read/analyze mode.
- Inspect source files, config docs, and tests before running commands.
- Do not run Python scripts just to see what happens.
- Only execute commands when the user explicitly asks for execution, or when execution is required to verify a concrete change you made.

## Entry Point Priority

Use the highest-level safe entry point available:

1. `./om-agent`
2. `./om`
3. `python3 -m ...`
4. `python3 scripts/...` only as a compatibility fallback

Do not default to `python3 scripts/...` for first-pass exploration.

## Safety Rules

- Never send real notifications unless the user explicitly requests it.
- Never modify production config files unless the user explicitly requests it.
- Never delete runtime artifacts, state files, reports, or output directories unless the user explicitly asks for cleanup.
- Prefer dry-run, validation, healthcheck, or test commands over live operational commands.
- If a command can mutate runtime state, write data, or send notifications, confirm that the user explicitly wants that action.

## Where To Look First

- Business logic: `domain/`
- Notification formatting: `scripts/notify_symbols.py`, `scripts/multi_tick/notify_format.py`
- Operator CLI behavior: `src/interfaces/cli/`, `./om`
- Agent entry points: `./om-agent`, `scripts/install_agent_plugin.sh`
- Config contracts: `CONFIGS.md`, `CONFIGURATION_GUIDE.md`, `configs/examples/`, `scripts/validate_config.py`

## Practical Defaults

- For system status or troubleshooting, prefer `./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'` before runtime commands.
- Use direct `python3 scripts/...` commands only when the user explicitly asks for that script, or no higher-level entry point covers the task.
- Keep account labels lowercase, such as `lx` and `sy`.
- Keep user-facing notifications Markdown-friendly and preserve the existing Chinese tone.
