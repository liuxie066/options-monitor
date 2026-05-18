# Agent Handbook — options-monitor

> This is the task-driven manual for agents working in `options-monitor`.
> Keep `AGENTS.md` short enough for prompt prefix use; put detailed execution guidance here.

## 1. Operating Model

`options-monitor` is an operations-sensitive local monitoring system for options strategies.
An agent should treat it as production tooling:

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
| MacBook Codex online-evidence handoff | `./om ai-cofunder collect ...` or `./om-agent run --tool ai_cofunder ...` |

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
| What strategy parameters look weak? | `strategy_replay_analyze` | Offline replay analysis, no config mutation |
| Is Sell Put cash constrained? | `query_cash_headroom` | Account-aware cash and collateral view |
| Is ledger projection trustworthy? | `option_positions_read action=inspect`, AI Cofunder `ledger` scope | Reads canonical event/projection state |
| Does close advice have inputs? | `prepare_close_advice_inputs`, then `close_advice` or `get_close_advice` | Keeps refresh and recommendation explicit |
| What evidence should MacBook Codex analyze? | `ai_cofunder` | Builds a redacted evidence bundle and handoff |

## 4. AI Cofunder Workflow

AI Cofunder is not an online AI product feature. The online/Linux side collects redacted evidence. MacBook Codex reads the handoff and helps diagnose quality issues, ledger problems, and strategy-improvement directions.

### Common Server Command

```bash
./om ai-cofunder collect \
  --config-key us \
  --scope full \
  --output both \
  --no-write-outputs
```

With scheduler evidence from the online job runner:

```bash
./om ai-cofunder collect \
  --config-key us \
  --scope full \
  --output both \
  --no-write-outputs \
  --scheduler-evidence-json '{"provider":"cron","job_name":"us-tick","last_status":"success","last_exit_code":0}'
```

With a readiness snapshot:

```bash
./om ai-cofunder collect \
  --config-key us \
  --scope full \
  --include-healthcheck \
  --no-write-outputs
```

Equivalent agent tool:

```bash
./om-agent run --tool ai_cofunder --input-json '{"config_key":"us","scope":"full","output":"both","write_outputs":false}'
```

### Scopes

| Scope | Purpose |
|---|---|
| `ledger` | Trade intake, position maintenance, and ledger quality evidence |
| `account-strategy` | Per-account strategy effects, candidate evidence, and filter traces |
| `quality` | Runtime freshness, latest run status, scheduler evidence, optional healthcheck |
| `strategy` | Candidate CSV, filter trace, and strategy replay evidence |
| `full` | Combined default |

Default runs do not write files. Writing reports requires `write_outputs=true`, `confirm=true`, and `OM_AGENT_ENABLE_WRITE_TOOLS=true`. Default output locations are:

```text
output_shared/ai_cofunder/
output_shared/state/current/ai_cofunder.current.json
```

MacBook SSH pattern:

```bash
ssh prod 'cd /path/to/options-monitor && ./om ai-cofunder collect \
  --config-key us \
  --scope full \
  --output handoff \
  --no-write-outputs' \
| python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["handoff_markdown"])'
```

Recommended Codex prompt:

```text
你现在作为 ai-cofunder。请基于下面的 AI Cofunder Handoff 分析线上质量问题，
重点看持仓/交易一致性、多账户对 sell put / sell call / YE 的影响，
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
| Default reports | `output/reports/` |
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

### Strategy Diagnostics

- Candidate ranking explanation: `src/application/agent_tool_candidate_rank.py`
- Filter trace explanation: `src/application/agent_tool_candidate_filter.py`
- Replay analysis: `src/application/agent_tool_strategy_replay.py`
- Docs: `docs/candidate_strategy.md`, `docs/STRATEGY_REPLAY.md`

For "why did this symbol/account not get a candidate", start from `candidate_filter_explain` and trace artifacts, not from final candidate CSV alone.

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
./om run tick
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
- Feishu `option_positions` is a mirror/sync surface only.
- Non-ledger runtime code must enter through `src/application/ledger/api.py`.
- Do not patch projected state directly when the canonical event chain is wrong.

### Close Advice

- Domain policy: `domain/domain/close_advice.py`
- Runner/I/O assembly: `src/application/close_advice_runner.py`
- Recommended agent entry: `get_close_advice`

Core domain functions:

```python
def evaluate_close_advice(inp: CloseAdviceInput, cfg: CloseAdviceConfig) -> dict[str, Any]: ...
def evaluate_close_optimizer(inp: CloseAdviceInput, cfg: CloseAdviceConfig) -> dict[str, Any]: ...
```

Keep new scoring or optimizer policy in the domain layer. The runner should stay focused on inputs, local artifacts, and output formatting.

### Notifications

- Per-account content: `src/application/notify_symbols.py`
- Multi-account wrapper: `src/application/multi_tick/notify_format.py`
- Preview tool: `preview_notification`

Notification text should remain Markdown-friendly and operationally direct. Do not send live notifications unless the user explicitly asks.

### Configuration

- Layered config build: `src/application/layered_config.py`
- Runtime validation: `src/application/config_validator.py`
- Examples: `configs/examples/user.example.us.json`, `configs/examples/user.example.hk.json`
- Full config docs: `CONFIGS.md`, `CONFIGURATION_GUIDE.md`

Do not weaken production config validation to make local tests pass. Fix the config path, test fixture, or validation contract instead.

### Agent Tools

- Manifest: `src/application/agent_tool_registry.py`
- Handlers: `src/application/agent_tool_handlers.py`
- Contracts: `src/application/agent_tool_contracts.py`
- Config helpers: `src/application/agent_tool_config.py`, `src/application/agent_tool_init_local.py`
- CLI: `src/interfaces/agent/cli.py` -> `./om-agent`

When adding or changing a tool, update manifest, handler, tests, and docs together.

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
3. Collect `ai_cofunder` handoff with `scope=full`.
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
4. Use `ai_cofunder` `account-strategy` or `full` scope for candidate/filter trace evidence.
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
| Agent manifest/handler | `python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py` |
| AI Cofunder | `python3 -m pytest tests/test_ai_cofunder.py` |
| Candidate filter/rank | Candidate engine tests, candidate tool tests, focused trace/replay tests |
| Tick orchestration | `python3 -m pytest tests/test_multi_tick_*.py tests/test_unified_tick_entrypoint.py` |
| Notifications | `python3 -m pytest tests/test_notify_symbols_markdown.py tests/test_multi_tick_notify_format.py` |
| Config | `python3 -m pytest tests/test_layered_config.py`; config build dry-runs |
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

## 11. Handoff Template

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
