# Agent Notes

This repository is primarily maintained for personal use. Keep agent support lightweight: prefer this shared guide plus the existing scripts instead of adding MCP servers, new agent-specific frameworks, or large directory restructures unless explicitly requested.

## Project Context

- Purpose: options monitoring and notifications for Sell Put and Covered Call workflows.
- Accounts: use lowercase account labels. Read the default list from top-level `accounts` in the runtime config; examples currently use `lx` and `sy`.
- Core code: `domain/` contains deterministic business logic; `scripts/` contains operational entry points.
- Reports: generated under `output/`, `output_accounts/`, and `output_shared/`.
- Notification layout source of truth: `scripts/notify_symbols.py` for per-account notification content and `scripts/multi_tick/notify_format.py` for merged/account message wrappers.

## Safety Rules

- Do not send real notifications unless the user explicitly asks for it.
- Do not deploy unless the user explicitly asks for it.
- Do not modify production config files unless the user explicitly asks for it.
- Do not delete runtime artifacts, state files, reports, or output directories unless the user explicitly asks for cleanup.
- Prefer dry-run modes for write operations, especially Feishu / option position writes.
- Preserve user changes in a dirty worktree. Never reset or revert unrelated files without explicit permission.

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

Run the multi-account scheduler entry point:

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
