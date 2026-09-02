你永远叫我棒棒的liuxie

# Agent Manual — options-monitor

> Operations-sensitive local options monitoring system.
> Treat this repo as a controlled production tool, not a sandbox.

## Repo-Specific Contract

- Treat current source, config, tests, and runtime artifacts as authority; memory and file names are only hints.
- Prefer root-cause fixes at the owning boundary. If a tactical patch is unavoidable, state the tradeoff and follow-up.
- Follow parsimony at repo boundaries: do not add entities, layers, states, tools, config keys, or workflows unless they are necessary.
- Preserve user changes in a dirty worktree. Never reset or revert unrelated files unless explicitly asked.
- Worktrees are conditional isolation, not a per-phase default. Reuse the same task worktree through implementation,
  validation, review, and PR; create another only for a protected/shared branch, unrelated dirty changes, or parallel conflict.
- After a task PR and required checks are terminal, the same task must close its exact worktree and local branch before
  declaring completion; for a release, wait until the tag, GitHub
  Release, target commit, and assets are verified. Require a clean status, proof that the branch is contained in
  `origin/main`, and no active owner before removing the exact worktree and local branch. Preserve dirty, unmerged,
  patch-equivalent-only, stashed, or owner-unknown work unless separately approved.
- Do not invoke Gateflow, planreview, or deepreview unless the user explicitly requests the named workflow or skill in the current task.

## DeepReview Profile

When the user explicitly invokes deepreview, apply these repository-specific rules in addition to the skill's general workflow:

- Review in this order, skipping irrelevant layers: strategy rationality, OpenD/provider data consistency, then code drift, duplication, and misplaced ownership.
- Start at the real public or runtime entry point and trace source -> normalization -> decision -> persistence/event/delivery. Documentation, filenames, cache rows, process exit, and scheduler success are not substitutes for authoritative facts.
- Preserve canonical owners and account, sender, market, and config isolation. Flag downstream fallback, duplicate calculation, loose parsing, or compatibility code that repairs an upstream or domain contract.
- Distinguish unavailable or partial data, stale cache, provider failure, model failure, and valid zero-result outcomes. Treat `delivery_confirmed` as stronger evidence than successful scheduling, rendering, or provider submission.
- For writes, prove preview, explicit confirmation, idempotency, one durable effect, readback, receipt, and safe retry or replay behavior, including ambiguous external outcomes.
- Treat tests as evidence only when they exercise the affected public facade and assert durable or externally visible behavior, including relevant failure, cancellation, stale-data, and retry paths.
- Report material confirmed defects separately from evidence gaps and unverified leads. Do not turn strategy preferences, speculative abstractions, or style opinions into findings.
- Keep review read-only. Do not scan providers, send notifications, trade, mutate ledgers or runtime state, or run commands with hidden writes without separate explicit authority.

## Project Identity

| Property | Value |
|---|---|
| Purpose | Cash-Secured Put (CSP) / Covered Call (CC) / Yield Enhancement scanning, filtering, reporting, and notification |
| Stack | Python 3, pandas, SQLite, OpenD/Futu API, Feishu webhooks |
| Accounts | Lowercase labels such as `lx`, `sy`; read from top-level `accounts` in runtime config |
| Canonical Configs | `config.yaml` is the human authoring source; `config.us.json` / `config.hk.json` are generated runtime snapshots |
| Reports | `output/`, `output_accounts/<account>/`, `output_shared/`, `output_runs/<run_id>/` |
| Process Artifacts | `docs/reviews/`, `docs/plans/`, `docs/gateflow/` are gitignored consumables; never `git add -f` them into the repo |
| Local Tool Gateway Entry | `./om-agent` |
| Human CLI Entry | `./om` |
| Detailed Agent Handbook | `docs/AGENT_WIKI.md` |

## Entry Point Ladder

Use the highest-level safe entry point available:

1. `./om-agent` for structured JSON tools and read-first diagnostics. It is the local Tool Gateway, not OM's autonomous Agent.
2. `./om` for human/operator CLI workflows.
3. `./.venv/bin/python -m src.application.<module>` only when no public facade exists.
4. `./.venv/bin/python scripts/...` only for compatibility or operational wrappers.

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
- Modify `config.yaml`, `config.us.json`, `config.hk.json`, secrets, or production runtime config.
- Delete `output/`, `output_runs/`, `output_shared/`, state files, caches, or runtime artifacts.
- Write Feishu, option-position state, trade events, or broker-facing data.

When a dry-run or read-only surface exists, use it first.

## Request Defaults

| User intent | Repo-specific default |
|---|---|
| explain / look into / check / why / how does this work | Start read-only; inspect source, docs, configs, tests, and runtime artifacts before proposing changes |
| commit and push / 提交并推送 | Commit and push the named development change only; do not modify `VERSION`, publish a Release, or upgrade production unless explicitly requested |
| release / 发布 | Prepare and publish the full VERSION-driven GitHub Release; production upgrade remains a separate explicit action |
| release and upgrade / 发布并升级远端 | Publish the VERSION-driven Release, then use the controlled remote upgrade and runtime verification flow |
| diagnostic only / 不要改文件 | Keep commands read-only and do not edit files |

Do not run Python scripts just to see what happens.

## Fast Diagnostic Commands

```bash
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'
```

Research evidence handoff for MacBook Codex:

```bash
./om research collect --config-key us --scope full --output both --no-write-outputs
```

## Module Ownership

| Task | Primary owner | Guardrail |
|---|---|---|
| Candidate filter/rank logic | `domain/domain/engine/candidate_engine.py` | Do not add parallel ranking in application scan adapters |
| Candidate trace / ranking diagnostics | `src/application/agent_tools/candidate_filter_impl.py`, `src/application/agent_tools/candidate_rank_impl.py` | Keep analysis read-only unless explicitly designing a write path |
| Notification text | `src/application/daily_decision_brief_renderer.py`; compatibility formatting in `src/application/notify_symbols.py` | Keep Markdown-friendly Chinese text; Daily Brief owns ordinary scheduled delivery |
| Close-advice policy | `domain/domain/close_advice.py` | Runner assembles I/O; scoring policy stays in domain |
| Option-position projection | `domain/domain/ledger/projection.py` | `trade_events -> projection -> position_lots` is canonical |
| Ledger application boundary | `src/application/ledger/api.py` | Non-ledger modules must not import ledger internals directly |
| Position workflows | `src/application/positions/` | Feishu `option_positions` mirror/sync is retired; SQLite ledger is authoritative |
| Trade intake/review | `src/application/trades/` | Preserve idempotency, review, void, and repair semantics |
| Tick orchestration | `src/application/multi_account_tick.py`, `src/application/multi_tick/` | Keep helper modules narrow |
| Runtime status / readiness | `src/application/agent_tools/runtime_status_impl.py`, `src/application/healthcheck.py` | Prefer extending read surfaces over adding hidden side effects |
| Research evidence | `src/application/research/` | Online side collects redacted evidence only; Codex performs analysis locally |
| Config validation | `src/application/config_validator.py`, `src/application/layered_config.py` | Do not weaken production config checks |
| Agent tools | `src/application/agent_tools/`, `src/application/agent_tool_registry.py` | Tool implementation/metadata live in domain `TOOLS`; registry collects them; write gates live in `agent_tools/permissions.py` |
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
# Candidate explanations
./om-agent run --tool candidate_filter_explain --input-json '{"run_id":"<run-id>","account":"lx","symbol":"NVDA"}'
./om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","top_n":5}'

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
./.venv/bin/python -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py

# Research
./.venv/bin/python -m pytest tests/test_research.py

# Notification formatting
./.venv/bin/python -m pytest tests/test_notify_symbols_markdown.py

# Tick behavior
./.venv/bin/python -m pytest tests/test_multi_tick_*.py tests/test_unified_tick_entrypoint.py

# Config validation
./.venv/bin/python -m pytest tests/test_layered_config.py
./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example
./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example
./om config build --source yaml --market us --config-yaml configs/examples/config.yaml.example --dry-run
./om config build --source yaml --market hk --config-yaml configs/examples/config.yaml.example --dry-run
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
