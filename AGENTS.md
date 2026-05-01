# Agent Notes

This repository is primarily maintained for personal use. Keep agent support lightweight: prefer this shared guide plus the existing scripts instead of adding MCP servers, new agent-specific frameworks, or large directory restructures unless explicitly requested.

## Project Context

- Purpose: options monitoring and notifications for Sell Put and Covered Call workflows.
- Accounts: use lowercase account labels. Read the default list from top-level `accounts` in the runtime config; examples currently use `lx` and `sy`.
- Core code: `domain/` contains deterministic business logic; `scripts/` contains operational entry points.
- Reports: generated under `output/`, `output_accounts/`, and `output_shared/`.
- Notification layout source of truth: `scripts/notify_symbols.py` for per-account notification content and `scripts/multi_tick/notify_format.py` for account message wrappers.

## Agent Operating Rules

- Treat this repository as an operations-sensitive tool, not a generic Python sandbox.
- If the user asks for explanation, investigation, or code reading, inspect files and summarize first. Do not run Python scripts just to see what happens.
- Only execute commands when the user explicitly asks for execution, or when execution is needed to verify a concrete code/config change you made.
- Prefer dry-run, validation, or test commands over live operational commands whenever both could answer the question.
- Before running a command that can send notifications, write data, or mutate runtime state, verify that the user explicitly requested that action.

## Research vs Execution

- Requests such as "how does this work", "look into", "check", "why", or "explain" default to read/analyze mode.
- In read/analyze mode, start from the relevant source files, config docs, and tests before considering command execution.
- Escalate from reading to execution only when static inspection is insufficient and the command is low risk, or when the user explicitly asks you to run it.

## Preferred Entry Points

- For structured agent/programmatic usage, prefer `./om-agent` first.
- For human-operated workflow commands, prefer the unified CLI `./om` when it covers the task.
- Use direct `python3 scripts/...` entry points only when:
  - the user explicitly asks for that script,
  - the unified CLI / agent CLI does not expose the needed capability,
  - or you are running tests / validation commands.
- When showing examples to users, prefer `./om-agent` or `./om` over raw script paths when both are valid.

## Task Routing Hints

- Business rules and deterministic calculations: inspect `domain/` first.
- Notification formatting and message layout: inspect `scripts/notify_symbols.py` and `scripts/multi_tick/notify_format.py` first.
- Operator-facing command behavior: inspect `src/interfaces/cli/`, `./om`, and related docs first.
- Agent-facing structured tooling: inspect `./om-agent`, `scripts/install_agent_plugin.sh`, and README agent sections first.
- Configuration contracts and runtime expectations: inspect `CONFIGS.md`, `CONFIGURATION_GUIDE.md`, examples under `configs/examples/`, and `scripts/validate_config.py` before executing config-related commands.

## Safety Rules

- Do not send real notifications unless the user explicitly asks for it.
- Do not deploy unless the user explicitly asks for it.
- Do not modify production config files unless the user explicitly asks for it.
- Do not delete runtime artifacts, state files, reports, or output directories unless the user explicitly asks for cleanup.
- Prefer dry-run modes for write operations, especially Feishu / option position writes.
- Preserve user changes in a dirty worktree. Never reset or revert unrelated files without explicit permission.

## Execution Guardrails for Claude Code / OpenClaw

- Do not default to running `python3 ...` commands for first-pass exploration.
- If a task can be answered by reading code, docs, config examples, or tests, do that first.
- When execution is necessary, choose the highest-level safe entry point available in this order:
  1. `./om-agent`
  2. `./om`
  3. `python3 -m ...`
  4. `python3 scripts/...`
- Prefer commands that validate or inspect state over commands that mutate state.
- If a live command is risky but a dry-run or test exists, use the dry-run or test first.

## Common Checks

For notification formatting changes:

```bash
python3 -m pytest tests/test_notify_symbols_markdown.py tests/test_multi_tick_notify_format.py
```

For multi-account or multi-tick behavior:

```bash
python3 -m pytest tests/test_multi_tick_*.py
```

For send gate / notification dispatch changes:

```bash
python3 -m pytest tests/test_send_if_needed_batch3.py tests/test_send_if_needed_batch4.py tests/test_send_if_needed_multi_deprecated_and_config_gate.py
```

Some send tests expect local runtime config files such as `config.us.json`. If those files are absent, report that clearly instead of treating it as a code failure.

For config validation:

```bash
python3 scripts/validate_config.py --config configs/examples/config.example.us.json
python3 scripts/validate_config.py --config configs/examples/config.example.hk.json
```

## Common Commands

Build per-account notification text from generated alerts:

```bash
python3 scripts/notify_symbols.py --alerts-input output/reports/symbols_alerts.txt --changes-input output/reports/symbols_changes.txt --output output/reports/symbols_notification.txt
```

Run the multi-account flow (preferred unified CLI):

```bash
./om run tick --config config.us.json --accounts lx sy
```

Compatibility launcher:

```bash
python3 scripts/send_if_needed_multi.py --config config.us.json --accounts lx sy
```

Query Sell Put cash usage:

```bash
python3 -m src.interfaces.cli.main sell-put-cash --market 富途 --account lx
python3 -m src.interfaces.cli.main sell-put-cash --market 富途 --account sy
```

Manage watchlist:

```bash
python3 scripts/watchlist.py list
python3 scripts/watchlist.py add TCOM --put
python3 scripts/watchlist.py edit TCOM --set sell_put.max_strike=45
python3 scripts/watchlist.py rm TCOM
```

Maintain option positions, using dry-run first:

```bash
python3 scripts/option_positions.py list --market 富途 --account lx --status open
python3 scripts/option_positions.py add --account lx --symbol 0700.HK --option-type put --side short --contracts 1 --currency HKD --strike 420 --multiplier 100 --exp 2026-04-29 --dry-run
```

## Style Notes

- Notification content should remain Markdown-friendly, not card-like plain text.
- Keep account labels lowercase: `lx`, `sy`.
- Keep missing data explicit. Do not invent values when upstream data is absent.
- Preserve the existing Chinese user-facing tone in reports and notifications.
