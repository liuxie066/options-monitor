# Ops Copilot Handbook - options-monitor

> This is the task-driven manual for Ops Copilot and local coding agents working in `options-monitor`.
> Keep `AGENTS.md` short enough for prompt prefix use; put detailed execution guidance here.

## 1. Operating Model

`options-monitor` is an operations-sensitive local monitoring system for options strategies.
Ops Copilot should treat it as production tooling:

- Inspect before changing.
- Prefer read-only tools before runtime commands.
- Keep production config, notification sends, Feishu writes, and broker-facing state behind explicit user intent.
- Use existing public facades before importing internals or calling scripts.
- Preserve unrelated local edits.

Primary entry points:

| Need | Entry |
|---|---|
| Structured tool call / JSON response | `./om-agent` |
| Human/operator command | `./om` |
| Runtime tick | `./om run tick ...` |
| Guarded production tick wrapper | `./om run tick-cron ...` |
| MacBook Codex online-evidence handoff | `./om research collect ...` |

For the canonical capability boundary, risk classes, Inbound exposure, and
verification map, see [OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md).

## 2. First Five Minutes

When entering an unfamiliar task, gather just enough context:

```bash
git status --short
rg -n "<user keyword>" README.md docs AGENTS.md src domain tests
./om-agent spec
```

For live quality or runtime questions, start with existing state:

```bash
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'
```

Do not run tick, send notifications, mutate positions, sync Feishu, or deploy unless the user explicitly asks for that side effect.

## 3. Tool Selection

Use the lowest-risk tool that can answer the question.

| Question | First tool or file | Why |
|---|---|---|
| Is the online run healthy? | `runtime_status` | Reads existing runtime artifacts without running pipelines |
| Can this environment run? | `healthcheck` | Validates readiness and dependencies |
| Did cron/tick decide to skip? | `scheduler_status`, `scheduler_decision.json` | Separates scheduler rules from cron execution |
| Why did a symbol disappear? | `candidate_filter_explain` | Uses trace evidence instead of guessing from final CSV |
| Why is candidate ranking odd? | `candidate_rank_explain` | Explains existing candidate CSV ranking |
| Is shadow replay evidence ready for tuning? | `research collect --scope candidate` | Offline candidate/reject universe readiness; no live config mutation |
| Is candidate evidence complete enough for scan diagnosis? | `healthcheck` / `doctor` with `candidate_evidence` inputs | Diagnostic row-count/readiness check, not a strategy recommendation |
| Is Sell Put cash constrained? | `query_cash_headroom` | Account-aware cash and collateral view |
| Is ledger projection trustworthy? | `option_positions_read action=inspect`, Research `ledger` scope | Reads canonical event/projection state |
| Does close advice have inputs? | `prepare_close_advice_inputs`, then `close_advice` or `get_close_advice` | Keeps refresh and recommendation explicit |
| What evidence should MacBook Codex analyze? | `research` | Builds a redacted evidence bundle and handoff |

## 4. Research / Shadow Replay Workflow

Research and Shadow Replay are an independent offline evidence/replay module.
They are not Ops Copilot core, not `./om-agent` tools, and not an online AI
product feature. The online/Linux side collects redacted evidence. MacBook Codex
reads the handoff and helps diagnose quality issues, ledger problems, and
strategy-improvement directions.

### Common Server Command

```bash
./om research collect \
  --config-key us \
  --scope full \
  --output both \
  --no-write-outputs
```

With scheduler evidence from the online job runner:

```bash
./om research collect \
  --config-key us \
  --scope full \
  --output both \
  --no-write-outputs \
  --scheduler-evidence-json '{"provider":"cron","job_name":"us-tick","last_run_id":"20260518T095446Z-2e7d54","last_triggered_at":"2026-05-18T09:54:46Z","last_status":"success","last_exit_code":0}'
```

With a readiness snapshot:

```bash
./om research collect \
  --config-key us \
  --scope full \
  --include-healthcheck \
  --no-write-outputs
```

### Scopes

| Scope | Purpose |
|---|---|
| `ledger` | Trade intake, position maintenance, and ledger quality evidence |
| `candidate` | Per-account candidate evidence, ranking samples, filter traces, and shadow replay readiness |
| `quality` | Runtime freshness, latest run status, scheduler evidence, optional healthcheck |
| `full` | Combined default |

Research keeps candidate CSVs separate from `*_candidates_reject_log.csv` files. Reject logs remain available as rejection evidence, but they must not inflate candidate row counts.

For offline strategy evidence review, inspect `candidate_evidence.shadow_replay` in the Research bundle, especially `review_readiness`. It is a readiness and analysis surface only; it cannot mutate scanner config. To compare how a concrete threshold hypothesis would change the observed candidate set, use `./om research shadow-replay candidate-impact-report --params <params.json>` or `--params-dir <dir>` against either an existing dataset or a `--profile-path` / date window; it writes paired JSON and Markdown candidate-impact reports. The older `parameter-report` command remains a compatibility entry. The underlying comparison stays inside `observed_run_universe`: if the requested start date has no scan artifacts, it must report coverage failure instead of reconstructing a historical option chain.

Default runs do not write files. Writing reports through `./om research collect`
requires `--write-outputs --confirm`. Default output locations are:

```text
output_shared/research/
output_shared/state/current/research.current.json
output_shared/research/shadow_replay/
```

MacBook SSH pattern:

```bash
ssh prod 'cd /path/to/options-monitor && ./om research collect \
  --config-key us \
  --scope full \
  --output handoff \
  --no-write-outputs' \
| python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["handoff_markdown"])'
```

Recommended Codex prompt:

```text
你现在作为 OM research analyst。请基于下面的 Research Handoff 分析线上质量问题，
重点看持仓/交易一致性、多账户对 sell put / covered call / YE 的影响，
输出：问题判断、证据、优先级、本地修复建议和需要补充的证据。
```

## 5. Runtime Evidence Map

Important runtime paths:

| Artifact | Path |
|---|---|
| Shared state | `output_shared/state/` |
| Current pointers | `output_shared/state/current/` |
| Per-account output | `output_accounts/<account>/` |
| Run snapshots | `output_runs/<run_id>/` |
| Default reports | `output_shared/reports/` |
| OpenD cache | `cache/opend_option_chain/`, `cache/opend_option_expirations/` |
| Audit logs | `audit/run_logs/` |

For runtime questions, prefer `runtime_status` because it already knows how to summarize these paths and distinguish latest run from latest scanned run.

## 6. Module Ownership

### Candidate Scanning

- Domain engine: `domain/domain/engine/candidate_engine.py`
- Application adapters: `src/application/candidate_scanning.py`, `src/application/scan_sell_put.py`, `src/application/scan_sell_call.py`
- Rule: do not add parallel ranking logic in application adapters.

Core domain functions:

```python
def evaluate_candidate_input(row: dict[str, Any]) -> dict[str, Any]: ...
def evaluate_candidate_hard_constraints(payload: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]: ...
def evaluate_candidate_return_floor(payload: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]: ...
def evaluate_candidate_risk_filter(payload: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]: ...
def rank_candidate_rows(rows: list[dict[str, Any]], *, mode: StrategyMode | str) -> list[dict[str, Any]]: ...
```

### Candidate Diagnostics

- Candidate ranking explanation: `src/application/agent_tool_candidate_rank.py`
- Filter trace explanation: `src/application/agent_tool_candidate_filter.py`
- Candidate evidence readiness: `healthcheck` / `doctor` `candidate_evidence` check
- Docs: `docs/candidate_strategy.md`

For "why did this symbol/account not get a candidate", start from `candidate_filter_explain` and trace artifacts, not from final candidate CSV alone.

For offline strategy evidence review, collect a candidate-scoped Research bundle first:

```bash
./om research collect --config-key us --scope candidate --run-id <run-id> --output json --no-write-outputs --shadow-replay-min-sample 30
```

Treat the shadow replay payload as offline evidence. If it lacks rejected samples, mark path snapshots, or outcome facts, it is not ready for manual strategy review and must not mutate production scanner config, Feishu, trade state, or notifications.

When remote storage is constrained, use `./om research archive pull --remote prod --ssh-target <host> --require-replay-evidence` first. The default local archive is `output_shared/research/remote_archive/prod/`; `pull` is dry-run unless `--write` is passed. `--require-replay-evidence` filters out scheduler skip / tick heartbeat directories and selects only runs with candidate CSV, reject log, or `candidate_filter_trace.jsonl`. After `./om research archive verify --remote prod`, use `./om research archive build-datasets --remote prod --market us --write` to create local Shadow Replay datasets; `--market` filters archived runs by inferred candidate/reject file market. Dataset build writes an initial scan-time mark from archived run `required_data/parsed` when present, but final outcome evidence still requires later path/expiry marks and `settle`. Only use `archive prune-remote --confirm` after the local `inventory.latest.json` proves every planned remote `output_runs` deletion has been verified locally.

For an explicit local dataset, use `./om research shadow-replay build --run-id <run-id>`, then inspect `./om research shadow-replay status --min-sample 30 --min-mark-points 2 --mark-stale-hours 24` to see each dataset's next data-lifecycle action. `data_plan` contains only executable data-maintenance actions (`collect_marks` / `settle`), while `review_queue` lists datasets ready for explicit manual `analyze`. Use `./om research shadow-replay run-data-plan` as the independent low-frequency maintenance entry: it is dry-run by default with no receipt write, and only `--write` executes eligible `collect_marks` / `settle` actions and writes a local receipt. It must not execute `analyze`; manual review stays on the explicit `analyze` command. Collect path samples with `./om research shadow-replay collect-marks --dataset <dataset-dir> --source local --write` or explicit OpenD sampling via `--source opend --write`. OpenD sampling refreshes local required-data cache before appending this point-in-time mark, and may update local OpenD rate-limit state / option-chain cache; it cannot recover past option marks that were never collected. OpenD preview without `--write` uses temporary paths and does not persist those files. You can still run the lower-level `mark`, `settle`, and `analyze` commands directly. Build, local collect, mark, and settle only write local replay evidence; OpenD collect also writes local evidence/cache files only. Missing required-data quotes are recorded as `missing_quote` evidence gaps and are not usable marks; expiry spot-only marks can be used for expiration outcome facts.

Use `outcome_by_bucket` from the analysis output to review DTE, Delta, IV/RV, spread, and concentration buckets before proposing filter or ranking changes.

For close/redeploy review, use existing `close_advice` output instead of creating a parallel optimizer. Treat `optimizer_switch` as advisory-only and require explicit alternative candidate evidence in the row, including `alternative_symbol`, `alternative_contract_symbol`, and `alternative_source_path`.

### Tick Runtime

- Orchestration spine: `src/application/multi_account_tick.py`
- Helper modules:
  - `tick_run_context`: idempotency bucket/key and completion records
  - `tick_guard_flow`: project guard, load shedding, market filter, OpenD phone-verify gate, watchdog admission
  - `tick_run_workspace`: run directory, required-data workspace, shared state pointer
  - `tick_scheduler_context`: trading-day guard, scheduler state path, scheduler decision
  - `tick_account_execution`: account defaults, worker limits, ordered concurrent execution, account metrics
  - `tick_notification_flow`: notification prep, quiet-hour decision, delivery, metrics, finalization

Tick flow:

```text
./om run tick --config <runtime-config.json>
-> src.application.multi_account_tick.run_tick
   -> tick_guard_flow
   -> tick_scheduler_context
   -> tick_account_execution
      -> expired position maintenance
      -> required_data prefetch
      -> pipeline_runtime / pipeline_watchlist / pipeline_symbol
      -> optional close advice
      -> per-account metrics and notification text
   -> tick_notification_flow
   -> run state and audit writes
```

Entrypoint signature:

```python
def run_tick(argv: list[str] | None = None) -> int: ...
```

### Ledger, Positions, And Trades

Canonical chain:

```text
trade_events
-> domain.domain.ledger.projection
-> position_lots
-> SQLite projection
-> optional Feishu mirror
```

Ownership:

| Area | Files |
|---|---|
| Domain projection | `domain/domain/ledger/projection.py` |
| Public application boundary | `src/application/ledger/api.py` |
| Use-case service | `src/application/ledger/service.py` |
| Repository/config boundary | `src/application/ledger/repository.py` |
| Stored event codec | `src/application/ledger/event_codec.py` |
| Event write and projection publish | `src/application/ledger/writer.py` |
| Manual trades | `src/application/ledger/manual_trades.py` |
| Void/repair interventions | `src/application/ledger/interventions.py` |
| Auto-close maintenance | `src/application/ledger/maintenance.py`, `src/application/positions/auto_close.py` |
| Position-facing workflows | `src/application/positions/` |
| Trade-facing workflows | `src/application/trades/` |

Core projection functions:

```python
def project_trade_events(events: list[TradeEvent]) -> ProjectionResult: ...
def build_risk_position_views(lots: list[PositionLot]) -> list[RiskPositionView]: ...
```

Rules:

- Local SQLite `trade_events` is the source of truth.
- Feishu `option_positions` is retired and must not be used for bootstrap, sync, or strategy reads.
- Non-ledger runtime code must enter through `src/application/ledger/api.py`.
- Do not patch projected state directly when the canonical event chain is wrong.

### Close Advice

- Domain policy: `domain/domain/close_advice.py`
- Runner/I/O assembly: `src/application/close_advice_runner.py`
- Recommended agent entry: `get_close_advice`

Core domain functions:

```python
def evaluate_close_advice(inp: CloseAdviceInput, cfg: CloseAdviceConfig) -> dict[str, Any]: ...
def evaluate_short_vol_close_advice(inp: CloseAdviceInput, ...) -> dict[str, Any]: ...
def evaluate_long_call_convexity_advice(inp: CloseAdviceInput, ...) -> dict[str, Any]: ...
def evaluate_close_optimizer(inp: CloseAdviceInput, cfg: CloseOptimizerConfig) -> dict[str, Any]: ...
```

Keep scoring, thesis checks, and exit-state policy in the domain layer. The
runner stays focused on loading local artifacts, pairing yield-enhancement legs,
preserving `not_evaluable` rows, and formatting CSV/text output.

### Notifications

- Per-account content: `src/application/notify_symbols.py`
- Multi-account wrapper: `src/application/multi_tick/notify_format.py`
- Preview tool: `preview_notification`

Notification text should remain Markdown-friendly and operationally direct. Do not send live notifications unless the user explicitly asks.

### Configuration

- YAML authoring: `src/application/config_yaml.py`, `src/application/config_yaml_init.py`
- Runtime snapshot validation: `src/application/config_validator.py`
- Legacy JSON compatibility: `src/application/layered_config.py`
- Examples: `configs/examples/config.yaml.example`, `configs/examples/user.example.us.json`, `configs/examples/user.example.hk.json`
- Full config docs: `CONFIGS.md`, `CONFIGURATION_GUIDE.md`

`config.yaml` is the human authoring surface. `config.us.json` and `config.hk.json` are generated runtime snapshots consumed by tick/agent tools. Legacy JSON user overlays are migration/upgrade-recovery inputs only; normal operator flows should use `config migrate-yaml`, then `config build --source yaml`.

Do not weaken production config validation to make local tests pass. Fix the config path, test fixture, or validation contract instead.

### Ops Copilot Tools

- Tool modules: `src/application/agent_tools/<domain>.py`
- Manifest collector: `src/application/agent_tool_registry.py`
- Write permission gate: `src/application/agent_tools/permissions.py`
- Contracts: `src/application/agent_tool_contracts.py`
- Config helpers: `src/application/agent_tool_config.py`, `src/application/agent_tool_init_local.py`
- CLI: `src/interfaces/agent/cli.py` -> `./om-agent`

When adding or changing a tool, put the implementation and manifest metadata in
the owning `agent_tools` domain module, then update focused tests and docs
together. Do not reintroduce a central handler switchboard.

## 7. Import Constraints

```text
domain/domain/        -> MUST NOT import src/ or scripts/
src/application/      -> MUST NOT import scripts/
src/infrastructure/   -> external adapters and persistence details
src/interfaces/       -> CLI/agent adaptation
scripts/              -> operational wrappers only; delegate to src/ or domain/
```

## 8. Common Investigation Playbooks

### Online Quality Looks Bad

1. Read `runtime_status`.
2. Add scheduler evidence if the issue involves cron or online jobs.
3. Collect `research` handoff with `scope=full`.
4. Inspect findings: scheduler, freshness, account failures, prefetch, notifications, maintenance, trade intake.
5. Only then decide whether to run focused local tests or modify code.

### A Symbol Is Missing

1. Get run/account/symbol from the user or runtime artifact.
2. Run `candidate_filter_explain`.
3. Compare market-level candidate evidence with account-level filters.
4. If account constraints are involved, inspect cash, holdings, and cost basis with `query_cash_headroom` and position tools.
5. Add a focused regression test around the leaking boundary if behavior is wrong.

### Multi-Account Strategy Behavior Looks Wrong

1. Confirm accounts are lowercase and present in runtime config.
2. Read `scheduler_status` per account.
3. Inspect `tick_metrics` through `runtime_status`.
4. Use `research` `candidate` or `full` scope for candidate/filter trace evidence.
5. Separate expected account constraints from state contamination.

### Ledger Or Trade Intake Looks Wrong

1. Use `option_positions_read action=inspect` or `action=events`.
2. Follow `trade_events -> projection -> position_lots`.
3. Check trade intake summaries and unresolved/failed counts in `runtime_status`.
4. Use semantic repair/void workflows; do not hand-edit projected rows.
5. Verify with focused ledger tests.

### Release Request

When the user asks to commit, push, and publish a remote release, assume the full release bundle:

1. Confirm intended file set with `git status --short`.
2. Update `VERSION` and `CHANGELOG.md`.
3. Run focused tests and release check.
4. Commit intended files only.
5. Push `main`.
6. Watch the `Release from VERSION` workflow.
7. Verify GitHub release and remote tag.

Use supported `gh release view --json` fields such as `tagName`, `name`, `url`, `publishedAt`, `targetCommitish`, `isDraft`, and `isPrerelease`.

## 9. Verification Matrix

| Change area | Suggested checks |
|---|---|
| Ops Copilot manifest/handler | `python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py` |
| Research | `python3 -m pytest tests/test_research.py` |
| Candidate filter/rank | Candidate engine tests, candidate tool tests, focused trace/replay tests |
| Tick orchestration | `python3 -m pytest tests/test_multi_tick_*.py tests/test_unified_tick_entrypoint.py` |
| Notifications | `python3 -m pytest tests/test_notify_symbols_markdown.py tests/test_multi_tick_notify_format.py` |
| Config | `python3 -m pytest tests/test_config_yaml.py tests/test_layered_config.py`; YAML validate/build dry-runs; runtime validate for generated snapshots |
| Ledger/positions/trades | Focused ledger, positions, and trade workflow tests |
| Docs only | `git diff --check`; verify referenced commands/tools exist when possible |

For type checking, prefer the narrow touched path first. Use broad checks when touching shared contracts.

## 10. Documentation Rules

- `AGENTS.md`: compact, stable, high-signal context for agents.
- `docs/AGENT_WIKI.md`: this task manual and code ownership map.
- `docs/TOOL_REFERENCE.md`: public `om-agent` tool contract and examples.
- `docs/AGENT_INTEGRATION.md`: JSON envelope and integration contract.
- `README.md`: human-facing product overview plus common operator commands.
- `RUNBOOK.md`: production cron, maintenance, and emergency operations.

When a public command, payload field, output path, or safety boundary changes, update the docs in the same change.

## 11. Memory / LLM Wiki Workflow

The project-level LLM wiki lives under `memory/`.

Use it as durable engineering context, not as a replacement for source code, tests, runtime evidence, or current architecture docs.

Authority order:

```text
Source code / tests / runtime evidence
> AGENTS.md / docs/ARCHITECTURE.md
> memory/decisions
> memory/patterns / memory/failures
> memory/sessions
```

At the start of architecture, reliability, release, or module-boundary work:

1. Read `memory/index.md`.
2. Open only the relevant memory entries.
3. Verify any drift-prone fact against current code, tests, config, or runtime artifacts.

Default behavior: do not automatically write memory during normal edits, normal debugging, or ordinary session summaries. Ingest only when the result creates durable knowledge:

- A new architecture decision or module ownership boundary.
- A public CLI, tool payload, output path, safety boundary, or runtime behavior change.
- A repeated or likely-to-repeat failure mode.
- A reusable implementation pattern proven by code and tests.
- A release or verification cycle with a stable operational lesson.

Before ingesting, decide whether the lesson is worth long-term promotion:

1. Will it change how a future agent designs, debugs, verifies, or releases work?
2. Is it backed by code, tests, config, runtime evidence, or a completed release?
3. Does it belong in durable memory instead of only `memory/sessions`?
4. Does it reduce future confusion more than it adds maintenance cost?

If the answer is unclear, keep it in the session handoff and do not promote it yet.

Manual prompts that may trigger ingest:

```text
ingest 这次改动
更新本次 session memory
请根据本次 session 更新 memory
memory lint
ingest this change into memory
update this session memory
please update memory from this session
```

When ingesting:

1. Read the relevant diff, tests, runtime evidence, and session summary.
2. Write the smallest useful entry in `memory/decisions`, `memory/patterns`, or `memory/failures`.
3. Use the templates in `memory/templates/`.
4. Update `memory/index.md` if future agents should discover the entry by module.
5. Append an audit note to `memory/log.md`.

Keep `memory/sessions` as short-term handoff material. Promote durable lessons out of session summaries before relying on them for future architecture decisions.

## 12. Handoff Template

Use this shape when handing work to another agent or future session:

```markdown
## Goal
What the user wanted.

## Current State
Files changed, tests run, known dirty unrelated files.

## Decisions
Why the chosen path fits the repo boundaries.

## Evidence
Commands, outputs, runtime artifacts, or failing tests.

## Next Steps
Smallest remaining actions, with blockers called out.
```
