# Agent Manual — options-monitor

> Operations-sensitive local options monitoring system.
> Treat this repo as a controlled production tool, not a sandbox.

## Collaboration Contract

- Work goal-first: define what "done" means, make the smallest useful change, then verify it.
- Be transparent: state assumptions, blockers, and risk before acting on unclear or dangerous steps.
- Split complex work into short steps. Keep unrelated cleanup out of the change.
- Prefer evidence from source, config, tests, and runtime artifacts over guesses from file names.
- Preserve user changes in a dirty worktree. Never reset or revert unrelated files unless explicitly asked.

## Project Identity

| Property | Value |
|---|---|
| Purpose | Sell Put / Covered Call / Yield Enhancement scanning, filtering, reporting, and notification |
| Stack | Python 3, pandas, SQLite, OpenD/Futu API, Feishu webhooks |
| Accounts | Lowercase labels such as `lx`, `sy`; read from top-level `accounts` in runtime config |
| Canonical Configs | `config.us.json`, `config.hk.json`; built from `configs/system.json` plus user overlays |
| Reports | `output/`, `output_accounts/<account>/`, `output_shared/`, `output_runs/<run_id>/` |
| Local Agent Entry | `./om-agent` |
| Human CLI Entry | `./om` |
| Detailed Agent Handbook | `docs/AGENT_WIKI.md` |

## Entry Point Ladder

Use the highest-level safe entry point available:

1. `./om-agent` for structured JSON tools and read-first diagnostics.
2. `./om` for human/operator CLI workflows.
3. `python3 -m src.application.<module>` only when no public facade exists.
4. `python3 scripts/...` only for compatibility or operational wrappers.

Unified tick chain:

```bash
./om run tick --config config.us.json --accounts lx [sy]
```

Production cron normally uses the guarded wrapper:

```bash
./om run tick-cron --market us --accounts lx sy --timeout 600
./om run tick-cron --market hk --accounts lx sy --timeout 600
```

Legacy `scripts/send_if_needed*.py` is removed. Do not use it.

## Safety Red Lines

Ask for explicit confirmation before any command that can:

- Send real notifications through Feishu, webhook, email, or another channel.
- Install, start, stop, or modify production services such as systemd / launchd units.
- Modify `config.us.json`, `config.hk.json`, secrets, or production runtime config.
- Delete `output/`, `output_runs/`, `output_shared/`, state files, caches, or runtime artifacts.
- Write Feishu, option-position state, trade events, or broker-facing data.

When a dry-run or read-only surface exists, use it first.

## Default Response By Request Type

| User intent | Default behavior |
|---|---|
| explain / look into / check / why / how does this work | Read/analyze first; inspect source, docs, configs, and tests before proposing changes |
| fix / add / change / run | Confirm dangerous scope if needed, implement the narrow change, then verify |
| release / 提交并推送 / 远端 release | Treat as full VERSION-driven release bundle unless the user says otherwise |
| diagnostic only / 不要改文件 | Keep commands read-only and do not edit files |

Do not run Python scripts just to see what happens.

## Fast Diagnostic Commands

```bash
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'
```

AI Cofunder evidence handoff for MacBook Codex:

```bash
./om ai-cofunder collect --config-key us --scope full --output both --no-write-outputs
```

## Module Ownership

| Task | Primary owner | Guardrail |
|---|---|---|
| Candidate filter/rank logic | `domain/domain/engine/candidate_engine.py` | Do not add parallel ranking in application scan adapters |
| Candidate trace / replay analysis | `src/application/agent_tool_candidate_filter.py`, `src/application/agent_tool_strategy_replay.py` | Keep analysis read-only unless explicitly designing a write path |
| Notification text | `src/application/notify_symbols.py`, `src/application/multi_tick/notify_format.py` | Keep Markdown-friendly Chinese text; no card-like plain text |
| Close-advice policy | `domain/domain/close_advice.py` | Runner assembles I/O; scoring policy stays in domain |
| Option-position projection | `domain/domain/ledger/projection.py` | `trade_events -> projection -> position_lots` is canonical |
| Ledger application boundary | `src/application/ledger/api.py` | Non-ledger modules must not import ledger internals directly |
| Position workflows | `src/application/positions/` | Feishu `option_positions` is mirror/sync, not source of truth |
| Trade intake/review | `src/application/trades/` | Preserve idempotency, review, void, and repair semantics |
| Tick orchestration | `src/application/multi_account_tick.py`, `src/application/multi_tick/` | Keep helper modules narrow |
| Runtime status / readiness | `src/application/agent_tool_openclaw.py`, `src/application/healthcheck.py` | Prefer extending read surfaces over adding hidden side effects |
| AI Cofunder evidence | `src/application/ai_cofunder/` | Online side collects redacted evidence only; Codex performs analysis locally |
| Config validation | `src/application/config_validator.py`, `src/application/layered_config.py` | Do not weaken production config checks |
| Agent tools | `src/application/agent_tool_registry.py`, `src/application/agent_tool_handlers.py` | Manifest, handler, and tests must stay in sync |
| CLI behavior | `src/interfaces/cli/main.py`, `src/interfaces/agent/cli.py` | Preserve public facade behavior where possible |

Business rules live in `domain/domain/`. That layer must not import `src/` or `scripts/`.

## Import Boundaries

```text
domain/domain/        -> MUST NOT import src/ or scripts/
src/application/      -> MUST NOT import scripts/
src/infrastructure/   -> external adapters and persistence details
src/interfaces/       -> CLI/agent request and response adaptation
scripts/              -> thin operational wrappers only
```

## Common Workflows

```bash
# Read-only runtime diagnosis
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'

# Candidate explanations
./om-agent run --tool candidate_filter_explain --input-json '{"run_id":"<run-id>","account":"lx","symbol":"NVDA"}'
./om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","top_n":5}'
./om-agent run --tool strategy_replay_analyze --input-json '{"replay_path":"output/reports/strategy_replay.csv","min_sample":5}'

# Cash and positions
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'

# Option-position write path: dry-run first
./om option-positions add --account lx --symbol NVDA --option-type put --side short --contracts 1 --currency USD --strike 100 --multiplier 100 --exp 2026-06-19 --dry-run
```

## Quality Gates

Use focused checks for the area touched, then add broader checks when risk warrants it.

```bash
# Agent contract
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py

# AI Cofunder
python3 -m pytest tests/test_ai_cofunder.py

# Notification formatting
python3 -m pytest tests/test_notify_symbols_markdown.py tests/test_multi_tick_notify_format.py

# Tick behavior
python3 -m pytest tests/test_multi_tick_*.py tests/test_unified_tick_entrypoint.py

# Config validation
python3 -m pytest tests/test_layered_config.py
./om config build --market us --user-config configs/examples/user.example.us.json --dry-run
./om config build --market hk --user-config configs/examples/user.example.hk.json --dry-run
```

For release work, also check `VERSION`, `CHANGELOG.md`, `scripts/release_check.py`, and `.github/workflows/release-from-version.yml`.

## Style Contract

- Account labels are lowercase: `lx`, `sy`.
- User-facing reports remain Markdown-friendly and preserve the existing Chinese tone.
- Missing data must be explicit; do not invent upstream values.
- Symbol canonicalization matters: use `NVDA`, `0700.HK`, `9992.HK`; aliases such as `POP` must not persist.
- Prefer small facade-preserving changes over public command churn.
- Update docs when a public command, tool payload, output path, or safety boundary changes.

---

<!-- DYNAMIC SECTION: content below may change between sessions -->

## Current Iteration Context

_Reserved for current sprint focus, recent refactors, and known blockers._
_See `docs/AGENT_WIKI.md` for the detailed agent handbook and `docs/SESSION_SUMMARY.md` for session handoff._
