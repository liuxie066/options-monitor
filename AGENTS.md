你永远叫我棒棒的liuxie

# Agent Manual — options-monitor

> Operations-sensitive local options monitoring system.
> Treat this repo as a controlled production tool, not a sandbox.

## Repo-Specific Contract

- Select evidence by question type using [Docs Index](docs/INDEX.md): implementation, effective runtime state, broker facts, and local ledger records have different owners. Preserve conflicts; memory and file names are only hints.
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
- Do not start Gateflow, planreview, or deepreview as independent workflows unless explicitly requested. An explicitly invoked workflow authorizes its prescribed internal reviews without requiring the user to name each nested skill; preserve that workflow's stage and production authorization boundaries.

## DeepReview Profile

When deepreview is explicitly requested or prescribed by an explicitly invoked workflow, apply these repository-specific rules in addition to the skill's general workflow:

- Review in this order, skipping irrelevant layers: strategy rationality, OpenD/provider data consistency, then code drift, duplication, and misplaced ownership.
- Start at the real public or runtime entry point and trace source -> normalization -> decision -> persistence/event/delivery. Documentation, filenames, cache rows, process exit, and scheduler success are not substitutes for authoritative facts.
- Preserve canonical owners and account, sender, market, and config isolation. Flag downstream fallback, duplicate calculation, loose parsing, or compatibility code that repairs an upstream or domain contract.
- Distinguish unavailable or partial data, stale cache, provider failure, model failure, and valid zero-result outcomes. Treat `delivery_confirmed` as stronger evidence than successful scheduling, rendering, or provider submission.
- For production or external persistent effects, prove preview, explicit authorization, idempotency, one durable effect, readback, receipt, and safe retry or replay behavior, including ambiguous external outcomes. Isolated development fixtures do not require an operator receipt.
- Use tests that would expose the affected behavior's regression. Domain calculations and invariants may use focused unit tests; entry-point, persistence, cross-module, or external-effect changes also need relevant facade or integration evidence. Cover failure, cancellation, stale-data, and retry paths where the changed contract requires them.
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

Read [Agent Handbook](docs/AGENT_WIKI.md) only for the relevant task: tool selection (§3), ownership (§6), investigation (§8), or verification (§9). Legacy `scripts/send_if_needed*.py` is removed; do not use it.

## Safety Red Lines

Require explicit authorization for the action, target, and scope before commands that can:

- Send real notifications through Feishu, webhook, email, or another channel.
- Install, start, stop, or modify production services such as systemd / launchd units.
- Modify secrets, production authoring configuration, or effective runtime configuration, including live `config.yaml`, `config.us.json`, and `config.hk.json`.
- Delete real reports, state, caches, or runtime artifacts, including `output/`, `output_runs/`, and `output_shared/`.
- Write Feishu, real option-position state, trade events, or broker-facing data.

Existing explicit authorization remains valid within its action, target, and scope; ask again only when those change or a controlled workflow requires confirmation of a specific preview. When a dry-run or read-only surface exists, use it first; authorization does not skip preview, readback, or receipt requirements.

Authorized local source/template edits and isolated test fixtures follow the development task. Before treating configuration or output as isolated, verify its path, consumers, and service bindings cannot affect a running environment or contact real services. When uncertain, keep it read-only and resolve the target before writing. Runtime JSON remains generated; edit its authoring source and use the supported build path.

## Request Defaults

| User intent | Repo-specific default |
|---|---|
| explain / look into / check / why / how does this work | Start read-only with the direct owner and evidence needed to answer; expand along the call chain when gaps remain. Inspect runtime only when the question concerns actual runtime behavior |
| commit and push / 提交并推送 | Commit and push the named development change only; do not modify `VERSION`, publish a Release, or upgrade production unless explicitly requested |
| release / 发布 | Prepare and publish the full VERSION-driven GitHub Release; production upgrade remains a separate explicit action |
| release and upgrade / 发布并升级远端 | Publish the VERSION-driven Release, then use the controlled remote upgrade and runtime verification flow |
| diagnostic only / 不要改文件 | Keep commands read-only and do not edit files |

Do not run Python scripts just to see what happens.

## Runtime Diagnosis

Bind the host, effective config/runtime root, market, account scope, and evidence time before runtime diagnostics. Examples using `us` or `lx` are not task defaults. Do not use local state as evidence for remote production, or treat missing/stale evidence as a valid empty result. Start with existing artifacts; run readiness checks or collect additional evidence only when needed to answer the question. See [Agent Handbook §2–3](docs/AGENT_WIKI.md#2-first-five-minutes).

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

## Quality and Delivery

Choose the smallest checks that can expose a regression in the touched behavior; expand to adjacent consumers for shared contracts and preserve required CI gates. Use [Agent Handbook §9](docs/AGENT_WIKI.md#9-verification-matrix) and the project `om-pre-push-checks` skill for delivery verification. Reuse passing evidence only while its code, tests, config, dependencies, generated inputs, base, and environment remain valid.

For development-instruction and documentation edits, verify meaning, references, formatting, and applicable guardrails. Runtime prompts and user-visible CLI/notification text are behavior changes and need their owning checks. Audit-only requests do not require tests merely to complete the audit; report evidence gaps.

Follow the shared Delivery workflow for explicitly authorized delivery stages. Release-specific commands and evidence are owned by [Release Process](docs/RELEASE_PROCESS.md); publishing and upgrading remain separate boundaries.

## Style Contract

- Account labels are lowercase: `lx`, `sy`.
- User-facing reports remain Markdown-friendly and preserve the existing Chinese tone.
- Missing data must be explicit; do not invent upstream values.
- Symbol canonicalization matters: use `NVDA`, `0700.HK`, `9992.HK`; aliases such as `POP` must not persist.
- Prefer small facade-preserving changes over public command churn.
- Update docs when a public command, tool payload, output path, or safety boundary changes.

Use `docs/SESSION_SUMMARY.md` only when a session handoff is needed; do not maintain a rolling task diary in this file.
