# Agent Context — options-monitor

> Operations-sensitive local options monitoring tool. Python + pandas + SQLite.
> Treat as a controlled operational system, not a generic sandbox.

## Project Identity

| Property | Value |
|---|---|
| Purpose | Sell Put / Covered Call option scanning, filtering, reporting, and notification |
| Stack | Python 3, pandas, SQLite, OpenD/Futu API, Feishu webhooks |
| Accounts | Lowercase labels: `lx`, `sy`. Read from top-level `accounts` in runtime config. |
| Canonical Configs | `config.us.json`, `config.hk.json` (built from `configs/system.json` + user overlays) |
| Reports | `output/`, `output_accounts/<account>/`, `output_shared/` |
| Notification Content | `src/application/notify_symbols.py` (per-account), `src/application/multi_tick/notify_format.py` (wrapper) |

## Module Map

| Task | Primary File(s) | Guardrails |
|---|---|---|
| Change candidate filter/rank logic | `domain/domain/engine/candidate_engine.py` | Do NOT add parallel ranking in `src/application/scan_*.py` |
| Change notification text format | `src/application/notify_symbols.py`, `src/application/multi_tick/notify_format.py` | Keep Markdown-friendly, not card-like plain text |
| Change close-advice policy | `domain/domain/close_advice.py` | Do NOT add scoring policy in `src/application/close_advice_runner.py` |
| Change option-position projection | `domain/domain/option_position_ledger.py` | Feishu `option_positions` is bootstrap/mirror only |
| Change tick orchestration | `src/application/multi_account_tick.py` | Keep subflows in narrow helper modules (see AGENT_WIKI) |
| Change config validation | `src/application/config_validator.py`, `src/application/layered_config.py` | — |
| Change agent tools | `src/application/agent_tool_registry.py`, `src/application/agent_tool_handlers.py` | — |
| Change CLI behavior | `src/interfaces/cli/main.py` (human), `src/interfaces/agent/cli.py` (agent) | Prefer facade-preserving changes |
| Read business rules | `domain/domain/` | Must not import `src/` or `scripts/` |

## Entry Point Hierarchy

Use the highest-level safe entry point available, in this order:

1. `./om-agent` — structured JSON tools (preferred for agents)
2. `./om` — human CLI
3. `python3 -m src.application.<module>` — module entry
4. `python3 scripts/...` — compatibility/operational wrappers only

Tick / scan / notification unified chain:
```bash
./om run tick --config config.us.json --accounts lx [sy]
```

Legacy `scripts/send_if_needed*.py` removed. Do not use.

## Safety Red Lines

Before executing any command, confirm user explicitly wants it if it involves:

- [ ] Sending real notifications (Feishu, webhook, email)
- [ ] Deploying to production (`make deploy-safe` or similar)
- [ ] Modifying `config.us.json` / `config.hk.json` or any production runtime config
- [ ] Deleting `output/`, `output_runs/`, `output_shared/`, state files, or runtime artifacts
- [ ] Any write operation without `--dry-run` first (especially Feishu / option-position writes)

Preserve user changes in a dirty worktree. Never reset or revert unrelated files without explicit permission.

## Default Mode

| User Request | Default Response |
|---|---|
| explain / look into / check / why / how does this work | Read/analyze mode: inspect source, docs, config examples, and tests first. Summarize before executing. |
| fix / add / change / run | Confirm scope, prefer `--dry-run` or tests, then execute. |

Do not run Python scripts just to see what happens.

## Style Contract

- Account labels lowercase: `lx`, `sy`
- User-facing reports remain Markdown-friendly; preserve existing Chinese tone
- Missing data explicit: do not invent values when upstream data is absent
- Symbol canonicalization: `NVDA`, `0700.HK`, `9992.HK`; aliases such as `POP` must not persist (use `src/application/opend_utils.py:resolve_underlier_alias`)

---

<!-- DYNAMIC SECTION: content below may change between sessions -->

## Current Iteration Context

_Reserved: current sprint focus, recent refactors, known blockers._
_See `docs/AGENT_WIKI.md` for detailed architecture and `docs/SESSION_SUMMARY.md` for session handoff template._

## Quick Checks

```bash
# Notification formatting
python3 -m pytest tests/test_notify_symbols_markdown.py tests/test_multi_tick_notify_format.py

# Tick behavior
python3 -m pytest tests/test_multi_tick_*.py tests/test_unified_tick_entrypoint.py

# Config validation
python3 -m pytest tests/test_layered_config.py
./om config build --market us --user-config configs/examples/user.example.us.json --dry-run
./om config build --market hk --user-config configs/examples/user.example.hk.json --dry-run
```

## Common Workflows

```bash
# Run unified tick
./om run tick --config config.us.json --accounts lx
./om run tick --config config.us.json --accounts lx sy

# Read-only diagnostics (preferred first step)
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'

# Sell Put cash headroom
python3 -m src.interfaces.cli.main sell-put-cash --market 富途 --account lx

# Watchlist management
./om watchlist list
./om watchlist add TCOM --put
./om watchlist edit TCOM --set sell_put.max_strike=45

# Option positions (dry-run first)
./om option-positions list --broker 富途 --account lx --status open
./om option-positions add --account lx --symbol 0700.HK --option-type put --side short --contracts 1 --currency HKD --strike 420 --multiplier 100 --exp 2026-04-29 --dry-run
```
