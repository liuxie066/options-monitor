# Agent Handbook - options-monitor

> This is the task-driven manual for local agents working in `options-monitor`.
> Keep `AGENTS.md` short enough for prompt prefix use; put detailed execution guidance here.

## 1. Operating Model

`options-monitor` is an operations-sensitive local monitoring system for options strategies.
Local agents should treat it as production tooling:

- Inspect before changing.
- Prefer read-only tools before runtime commands.
- Keep production config, notification sends, Feishu writes, and broker-facing state behind explicit user intent.
- Use existing public facades before importing internals or calling scripts.
- Preserve unrelated local edits.

Primary entry points:

| Need | Entry |
|---|---|
| Structured tool call / JSON response | `./om-agent` Tool Gateway |
| Local or remote message handling | `./om assistant handle` Inbound Assistant |
| Human/operator command | `./om` |
| Runtime tick | `./om run tick ...` |
| Guarded production tick wrapper | `./om run tick-cron ...` |
| MacBook Codex online-evidence handoff | `./om research collect ...` |

Entrypoint rule:

- Use `./om-agent` for structured local JSON tool calls, manifest checks, and
  read-first diagnostics.
- Use `./om assistant handle` for local or remote messages. This is the
  Inbound Assistant surface.
- Explicit commands and pending-operation replies use deterministic Control.
  Every other message enters the single read-only `om_chat` Copilot Scene when
  `assistant.copilot.enabled` is true. There is no business router, per-Scene
  channel allowlist, planner fallback, or write-capable model path.

For the canonical entry and layer boundaries, see
[ARCHITECTURE.md](ARCHITECTURE.md) and [INBOUND_CONTROL.md](INBOUND_CONTROL.md).
For capability boundaries, risk classes, Inbound Assistant exposure, and
verification maps, see [OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md).

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

For explicit Control operation diagnosis, read the durable operation timeline:

```bash
./om-agent run --tool operation_timeline --input-json '{"limit":10}'
```

Copilot sessions, runs, and model/tool events are owned by the Copilot Host
store. Control audit rows must not be repackaged as synthetic Agent plans or
evidence sessions.

## 3. Tool Selection

Use the lowest-risk tool that can answer the question.

| Question | First tool or file | Why |
|---|---|---|
| Is the online run healthy? | `runtime_status` | Reads existing runtime artifacts without running pipelines |
| Can this environment run? | `healthcheck` | Validates readiness and dependencies |
| Did cron/tick decide to skip? | `scheduler_status`, `scheduler_decision.json` | Separates scheduler rules from cron execution |
| Why did a symbol disappear? | `symbol_resolve` if identity is unclear, then `candidate_filter_explain` | Uses trace evidence instead of guessing from final CSV |
| Why is candidate ranking odd? | `candidate_rank_explain` | Explains existing candidate CSV ranking |
| Is shadow replay evidence ready for tuning? | `research collect --scope candidate` | Offline candidate/reject universe readiness; no live config mutation |
| Is candidate evidence complete enough for scan diagnosis? | `healthcheck` / `doctor` with `candidate_evidence` inputs | Diagnostic row-count/readiness check, not a strategy recommendation |
| Is Sell Put cash constrained? | `query_cash_headroom` | Account-aware cash and collateral view |
| Is ledger projection trustworthy? | `option_positions_read action=inspect`, Research `ledger` scope | Reads canonical event/projection state |
| Does close advice have inputs? | `prepare_close_advice_inputs`, then `close_advice` or `get_close_advice` | Keeps refresh and recommendation explicit |
| What evidence should MacBook Codex analyze? | `research` | Builds a redacted evidence bundle and handoff |

## 4. Research / Shadow Replay Workflow

Research and Shadow Replay are an independent offline evidence/replay module.
They are not Inbound Assistant core, not `./om-agent` tools, and not an online AI
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
| `candidate` | Per-account candidate evidence, ranking samples, filter traces, Combo Yield pair rejection funnel / nearest misses, and shadow replay readiness |
| `quality` | Runtime freshness, latest run status, scheduler evidence, optional healthcheck |
| `full` | Combined default |

Research keeps candidate CSVs separate from `*_candidates_reject_log.csv` files. Reject logs remain available as rejection evidence, but they must not inflate candidate row counts.

For offline strategy evidence review, inspect `candidate_evidence.shadow_replay` in the Research bundle, especially `review_readiness`. It is a readiness and analysis surface only; it cannot mutate scanner config. To compare how a concrete threshold hypothesis would change the observed candidate set, use `./om research shadow-replay candidate-impact-report --params <params.json>` or `--params-dir <dir>` against either an existing dataset or a `--profile-path` / date window; it writes paired JSON and Markdown candidate-impact reports. The underlying comparison stays inside `observed_run_universe`: if the requested start date has no scan artifacts, it must report coverage failure instead of reconstructing a historical option chain.

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
| ./.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["data"]["handoff_markdown"])'
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

- Candidate ranking explanation: `src/application/agent_tools/candidate_rank_impl.py`
- Filter trace explanation: `src/application/agent_tools/candidate_filter_impl.py`
- Candidate evidence readiness: `healthcheck` / `doctor` `candidate_evidence` check
- Docs: `docs/candidate_strategy.md`

For "why did this symbol/account not get a candidate", start from `candidate_filter_explain` and trace artifacts, not from final candidate CSV alone. If the user gives a Chinese name or alias, resolve it with `symbol_resolve` or pass the raw alias to `candidate_filter_explain`; `account` is scan scope, not symbol identity. The tool discovers traces from the runtime root, latest-run pointer, recent `output_runs`, and shared-report fallbacks; only pass explicit paths for manual forensics.

For offline strategy evidence review, collect a candidate-scoped Research bundle first:

```bash
./om research collect --config-key us --scope candidate --run-id <run-id> --output json --no-write-outputs --shadow-replay-min-sample 30
```

Treat the shadow replay payload as offline evidence. If it lacks rejected samples, mark path snapshots, or outcome facts, it is not ready for manual strategy review and must not mutate production scanner config, Feishu, trade state, or notifications.

When remote storage is constrained, use `./om research archive pull --remote prod --ssh-target <host> --require-replay-evidence` first. The default local archive is `output_shared/research/remote_archive/prod/`; `pull` is dry-run unless `--write` is passed. `--require-replay-evidence` filters out scheduler skip / tick heartbeat directories and selects only runs with candidate CSV, reject log, or `candidate_filter_trace.jsonl`. After `./om research archive verify --remote prod`, use `./om research archive build-datasets --remote prod --market us --write` to create local Shadow Replay datasets; `--market` filters archived runs by inferred candidate/reject file market. Dataset build writes an initial scan-time mark from archived run `required_data/parsed` when present, but final outcome evidence still requires later path/expiry marks and `settle`. Only use `archive prune-remote --confirm` after the local `inventory.latest.json` proves every planned remote `output_runs` deletion has been verified locally.

For an explicit local dataset, use `./om research shadow-replay build --run-id <run-id>`, then inspect `./om research shadow-replay status --min-sample 30 --min-mark-points 2 --mark-stale-hours 24` to see each dataset's next data-lifecycle action. `data_plan` contains only executable data-maintenance actions (`collect_marks` / `settle`), while `review_queue` lists datasets ready for explicit manual `analyze`. Use `./om research shadow-replay run-data-plan` as the independent low-frequency maintenance entry: it is dry-run by default with no receipt write, and only `--write` executes eligible `collect_marks` / `settle` actions and writes a local receipt. It must not execute `analyze`; manual review stays on the explicit `analyze` command. Collect path samples with `./om research shadow-replay collect-marks --dataset <dataset-dir> --source local --write` or explicit OpenD sampling via `--source opend --write`. OpenD sampling refreshes local required-data cache before appending this point-in-time mark, and may update local OpenD rate-limit state / option-chain cache; it cannot recover past option marks that were never collected. OpenD preview without `--write` uses temporary paths and does not persist those files. You can still run the lower-level `mark`, `settle`, and `analyze` commands directly. Build, local collect, mark, and settle only write local replay evidence; OpenD collect also writes local evidence/cache files only. Missing required-data quotes are recorded as `missing_quote` evidence gaps and are not usable marks; expiry spot-only marks can be used for expiration outcome facts.

Use `outcome_by_bucket` from the analysis output to review DTE, Delta, IV/RV, spread, and concentration buckets before proposing filter or ranking changes.

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
./om run tick --config <runtime-config.json>  # manual scan; no ordinary Tick auto-send
-> src.application.multi_account_tick.run_tick
   -> tick_guard_flow
   -> tick_scheduler_context
   -> tick_account_execution
      -> expired position maintenance
      -> required_data prefetch
      -> pipeline_runtime / pipeline_watchlist / pipeline_symbol
      -> optional close advice
      -> per-account metrics and notification text
   -> tick_notification_flow  # scheduled only: Daily Decision Brief ordinary delivery
   -> run state and audit writes
```

Direct `run tick` calls, including `--force`, still produce scan/run artifacts but do not auto-send ordinary Tick notifications. Use the guarded `run tick-cron` entry for scheduled ordinary delivery. `symbols_notification.txt` is a Compact compatibility bundle that may also contain candidate rejection summary and Close Advice sections; it is not evidence that a Daily Brief was prepared or sent. Public runtime reads expose it canonically as `compatibility_notification` with `authority=compatibility_only` and `delivery_evidence=false`; the old `notification` fields are deprecated Phase A/B aliases scheduled for removal in Phase C.

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
```

Ownership:

| Area | Files |
|---|---|
| Domain projection | `domain/domain/ledger/projection.py` |
| Public application boundary | `src/application/ledger/api.py` |
| Use-case commands | `src/application/ledger/commands.py` |
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

#### Option Performance And Portfolio Bridges

Primary read entry points:

```bash
./om option-performance report --config-key us --account lx --period mtd
./om option-performance report --config-key us --account lx --period ytd --as-of-date 2026-07-17
./om option-performance cash-conversion backfill --config-key us --account lx --start-date 2026-04-01 --end-date 2026-07-24
./om-agent run --tool option_performance_report --input-json '{"config_key":"us","account":"lx","period":"month","month":"2026-06"}'
PORTFOLIO_SERVICE_URL=http://127.0.0.1:8765 ./om-agent run --tool portfolio_pnl_bridge --input-json '{"period":"mtd","as_of_month":"2026-07","accounts":["lx","sy"]}'
PORTFOLIO_SERVICE_URL=http://127.0.0.1:8765 ./om-agent run --tool portfolio_cash_bridge --input-json '{"period":"mtd","as_of_month":"2026-07","accounts":["lx","sy"]}'
```

Use the metric namespace that matches the question:

- profit / earnings -> `pnl.period_total_net` or an explicit gross/realized variant;
- cash movement -> `cash.total_cash_change_net` and its six components;
- premium activity -> `activity.premium_collected_gross`;
- capital efficiency -> the explicit `capital.*_annualized_efficiency` fields only.

`premium_collected_gross` is not additional profit. Assignment stock principal is cash movement and an asset conversion, not option PnL. Missing fee, mark, or FX evidence stays partial/null and must never be replaced with zero. A configured account scope with no events is a proven observed zero; an arbitrary unconfigured scope remains `not_observed`.

Cash backfill reads persisted event-time FX evidence, defaults to dry-run, and
requires `--apply` for the atomic ledger enrichment plus audit receipt. It
never replaces an already observed `cash_conversion.v1`.

`monthly_income_report`, `./om option-positions report monthly-income`, and
`portfolio_capital_bridge` have been removed. Do not recreate their ambiguous
`net_income_cny` or generic return fields. The migration note is historical
mapping only, not a callable rollback path.

### Close Advice

- Domain policy: `domain/domain/close_advice.py`
- Runner/I/O assembly: `src/application/close_advice_runner.py`
- Recommended agent entry: `get_close_advice`

Core domain functions:

```python
def evaluate_close_advice(inp: CloseAdviceInput, cfg: CloseAdviceConfig) -> dict[str, Any]: ...
def evaluate_short_vol_close_advice(inp: CloseAdviceInput, ...) -> dict[str, Any]: ...
def evaluate_long_call_convexity_advice(inp: CloseAdviceInput, ...) -> dict[str, Any]: ...
```

Keep scoring, thesis checks, and exit-state policy in the domain layer. The
runner stays focused on loading local artifacts, pairing yield-enhancement legs,
preserving `not_evaluable` rows, and formatting CSV/text output.

### Notifications

- Per-account content: `src/application/notify_symbols.py`
- Multi-account wrapper: `src/application/multi_tick/notify_format.py`
- Shared System Notice / Receipt presentation shell: `src/application/notification_shells.py`
- Preview tool: `preview_notification`
- Perception audit card: `assistant_perception` events written by
  `src/application/tick_notification_flow.py`
- Read tool: `notification_perception_read`

Notification text should remain Markdown-friendly and operationally direct. The business renderer owns one canonical Markdown string: proactive Feishu App delivery projects it as `msg_type=post` with no duplicate `title`, splitting blank/spacer-only lines into native `zh_cn.content` paragraphs. Content paragraphs each contain one `md` node; separators become dedicated plain-text spacer paragraphs so the visual blank line remains without prefixing the following desktop Markdown with a zero-width character. WeChat ClawBot sends the same canonical string unchanged through `text_item.text`. Feishu inbound replies/outbox remain text. Do not create channel-specific business renderers or parse/rewrite the Markdown in an adapter beyond this paragraph projection.

Scheduled ordinary delivery has one renderer authority: Daily Decision Brief. `preview_notification` is read-only and defaults to the Compact compatibility renderer; its output always reports `authority=compatibility_only` and `delivery_evidence=false`. Explicit `render_style=legacy` remains temporarily available only for compatibility inspection and returns a deprecation warning. Neither preview renderer may be used as a scheduled fallback.

System notices use `# OM · 系统通知 · <component>` and receipts use `# OM · 回执 · <account>` plus `类型｜成交` or `类型｜持仓维护`. `notification_shells.py` owns only the flat Markdown H1/field/section layout. OpenD rate limits and recovery, delivery-failure aggregation/retry, trade receipt warnings, and maintenance receipt status/dedupe/persistence remain with their existing callers; the shell must not send, retry, inspect provider byte limits, or classify business state.

Feishu post delivery measures the exact final outer JSON request body as UTF-8 before token acquisition or message HTTP. Requests over the fixed 28 KiB local budget fail closed as `FEISHU_POST_TOO_LARGE`, retaining only byte counts, normalized character count, and a SHA-256 content hash. Do not truncate, fragment, retry this deterministic local failure, or automatically fall back to text. Timeouts, transient failures, confirmed sends, and ambiguous sends must also never trigger text fallback for the same business event. Live desktop/mobile canaries and any rollback to the text sender require separate explicit operator approval; after rollback, only an HTTP-before-send size failure may be explicitly replayed with a new transport UUID and linked audit.

Notification perception events are compressed system evidence for Assistant
follow-ups. They record delivery action/reason, accounts, symbol summaries,
message lengths and hashes, but not raw notification text or webhook secrets.
They may enter ClawBot conversation context as `system_event` evidence; they
must not be treated as user messages or as authorization to write config, send
notifications, or mutate broker-facing state.

### Configuration

- YAML authoring: `src/application/config_yaml.py`, `src/application/config_yaml_init.py`
- Runtime snapshot validation: `src/application/config_validator.py`
- Legacy JSON migration reader: `src/application/layered_config.py`
- Examples: `configs/examples/config.yaml.example`, `configs/examples/user.example.us.json`, `configs/examples/user.example.hk.json`
- Full config docs: `CONFIGS.md`, `CONFIGURATION_GUIDE.md`

`config.yaml` is the human authoring surface. `config.us.json` and `config.hk.json` are generated runtime snapshots consumed by tick/agent tools. Legacy JSON user overlays are one-time `config migrate-yaml` inputs only, not an upgrade-recovery path; production upgrade fails closed when the YAML authoring source is unavailable.

Do not weaken production config validation to make local tests pass. Fix the config path, test fixture, or validation contract instead.

### Tool Gateway Tools

- Tool modules: `src/application/agent_tools/<domain>.py`
- Manifest collector: `src/application/agent_tool_registry.py`
- Write permission gate: `src/application/agent_tools/permissions.py`
- Contracts: `src/application/agent_tool_contracts.py`
- Config helpers: `src/application/agent_tool_config.py`, `src/application/agent_tool_init_local.py`
- CLI: `src/interfaces/agent/cli.py` -> `./om-agent`

When adding or changing a tool, put the implementation and manifest metadata in
the owning `agent_tools` domain module, then update focused tests and docs
together. Root-level `src/application/agent_tool_*.py` files, except shared
config/contract/registry helpers, are compatibility re-export shims only. Do
not reintroduce a central handler switchboard.

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
2. Resolve natural-language or alias symbols with `symbol_resolve` when needed.
3. Run `candidate_filter_explain`.
4. Compare market-level candidate evidence with account-level filters.
5. If account constraints are involved, inspect cash, holdings, and cost basis with `query_cash_headroom` and position tools.
6. Add a focused regression test around the leaking boundary if behavior is wrong.

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

Development delivery and release publication are separate:

- `commit and push` / `提交并推送` means validate, commit, and push the named development change. Update
  `CHANGELOG.md / Unreleased` when the change belongs in user-facing release notes, but do not modify
  `VERSION`, create a tag or Release, or upgrade production.
- `merge main` / `合并 main` integrates a complete, green change into the next release candidate. It still
  does not publish or deploy a version.
- `release` / `发布` means prepare and publish the VERSION-driven GitHub Release. It does not upgrade
  production unless the request explicitly includes the remote upgrade.
- `release and upgrade` / `发布并升级远端` includes the controlled production upgrade and post-upgrade
  runtime verification.

When the user explicitly asks to publish a release, execute the full publication bundle:

1. Confirm intended file set with `git status --short`.
2. Review all commits since the latest release tag against `CHANGELOG.md / Unreleased`.
3. Preview the automatic version recommendation and rendered release notes.
4. Move `Unreleased` items into the dated target-version section and update `VERSION`.
5. Run focused tests and strict release checks.
6. Commit the version metadata as `chore: release <version>`.
7. Push `main`.
8. Watch the `Release from VERSION` workflow.
9. Verify the GitHub Release, remote tag, target commit, and assets.

Use supported `gh release view --json` fields such as `tagName`, `name`, `url`, `publishedAt`, `targetCommitish`, `isDraft`, and `isPrerelease`.

## 9. Verification Matrix

| Change area | Suggested checks |
|---|---|
| Tool Gateway manifest/handler | `./.venv/bin/python -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py` |
| Research | `./.venv/bin/python -m pytest tests/test_research.py` |
| Candidate filter/rank | Candidate engine tests, candidate tool tests, focused trace/replay tests |
| Tick orchestration | `./.venv/bin/python -m pytest tests/test_multi_tick_*.py tests/test_unified_tick_entrypoint.py` |
| Notifications | `./.venv/bin/python -m pytest tests/test_notify_symbols_markdown.py tests/test_multi_tick_notify_format.py` |
| Config | `./.venv/bin/python -m pytest tests/test_config_yaml.py tests/test_layered_config.py`; YAML validate/build dry-runs; runtime validate for generated snapshots |
| Ledger/positions/trades | Focused ledger, positions, and trade workflow tests |
| Docs only | `git diff --check`; verify referenced commands/tools exist when possible |

For type checking, prefer the narrow touched path first. Use broad checks when touching shared contracts.

## 10. Documentation Rules

- `AGENTS.md`: compact, stable, high-signal context for agents.
- `docs/AGENT_WIKI.md`: this task manual and code ownership map.
- `docs/ARCHITECTURE.md`: current system architecture and entry boundaries.
- `docs/INBOUND_CONTROL.md`: controlled channel message entry boundary.
- `docs/TOOL_REFERENCE.md`: public `om-agent` Tool Gateway contract and examples.
- `docs/AGENT_INTEGRATION.md`: Tool Gateway JSON envelope and integration contract.
- `README.md`: human-facing product overview plus common operator commands.
- `RUNBOOK.md`: production cron, maintenance, and emergency operations.

When a public command, payload field, output path, or safety boundary changes, update the docs in the same change.

## 11. Archived Memory Reference

The `memory/` tree is archived project reference material, not an active LLM wiki workflow.

Use it only when a task needs historical context or prior decisions. Start from `memory/index.md`, open only relevant entries, and verify drift-prone facts against current source, tests, config, docs, or runtime artifacts before acting.

Do not use memory as a standing ingest target. Normal work should not add entries, update `memory/index.md`, append to `memory/log.md`, or use archived templates. Prefer updating current docs, tests, or runtime read surfaces when behavior or boundaries change.

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

## Option notification read and delivery model

`daily_decision_brief.v1` is the immutable account+market+trading-date successful-scan model. Delivery v2 separately owns fixed-target confirmation, pending/alerted candidate identities, and exact retry envelopes.

- Renderer authority: scheduled automatic ordinary notifications use Daily Brief only. Compact/Legacy has no scheduled sender authority. The deprecated `notifications.daily_brief.enabled` key is accepted with a stable warning during compatibility but its value does not change routing.
- Scheduler: keep the 10-minute wake-up. Canonical scans run only at `09:40`, eligible whole hours, eligible `HH:30`, and `15:50`; `09:30`, lunch breaks, and other wake-ups do not scan. A process failure relies on a later eligible scheduler slot; it does not invent an off-schedule retry scan.
- Fixed reports: `09:40`, eligible whole hours, and `15:50` prepare a full user report even with no candidates. A fixed failure prepares an explicit failure report and never projects the previous successful current as this round's result.
- Candidate alerts: eligible half-hour successful scans send immediately only when `current candidate identities - alerted identities` is non-empty. If fixed-report and new-candidate conditions coincide, the single complete fixed report wins.
- Trigger safety: manual/force reliable scans may advance the successful current snapshot for later query and candidate recovery, but they do not create an ordinary delivery envelope, resolve a provider route, or send an ordinary notification. Scheduled display uses the structured target; manual/force never infer a batch from reason text.
- Persistence order: durable successful outcome or fixed-failure evidence plus exact envelope -> exact scheduled-target watermark -> provider send -> attempt/ambiguous/confirmed transition.
- Retry: no-scan wake-ups may replay only an already persisted exact envelope. They must not run broker access, pipeline, assembler, candidate detection, revision persistence, or message re-rendering.
- Successful current: ready/degraded reliable scans advance current; failed/blocked/no-op scans do not. Query always reads the latest successful current, never the last delivered message.
- Funds: render `cash_total_by_currency`, `option_opening_available_by_currency`, and candidate-scoped capacity. Never display total assets, NAV, securities market value, or `0` for unknown funds. Sell Put capacities share account cash and cannot be summed.
- Time and identity: scheduled batch and actual data-as-of are separate renderer inputs. Transient display time does not enter the persisted brief digest, candidate identity, or delivery confirmation pointer.
- Candidate event authority: user event facts come only from the same run's `output_runs/<run_id>/state/event_snapshot.json`. Missing, malformed, stale, partial, conflicting, or degraded evidence remains unable-to-confirm; it never falls back to candidate CSV compatibility fields and never changes candidate identity, ranking, eligibility, or capacity.
- User projection: fixed report, candidate alert, fixed failure, and query share the Daily Brief human contract. Markdown hides revision, internal IDs, broker codes, raw enums, raw ISO timestamps, paths, and rejection dumps while structured artifacts retain them.
- Query scope: latest accepts optional account and market. Missing filters are resolved from canonical `config.us.json` / `config.hk.json`, then rendered by account and market without combining funds. Day/revision reads remain explicit operator queries requiring an account; market keeps the existing US default when omitted.
- Query safety: query is byte-for-byte read-only with respect to delivery state and does not refresh data, scan, send, confirm, or mutate candidate state.
- Delivery ambiguity: ambiguous envelopes are frozen. Later attempts either replay the exact message/key/hash under the provider idempotency contract or wait for explicit confirmation.
- Multi-market: an explicit combined-market tick is terminal fail-closed before Daily Brief assemble, revision/current persistence, delivery-envelope creation, or provider work. Production scheduled runs remain single-market.
- Rollout safety: release, remote upgrade, production pointer migration, real-send canary, and scheduler observation require separate operator authorization. Rollback stops the scheduler and rolls back code/version plus compatible state; it never restores Compact as a parallel scheduled sender.

Read surfaces:

```bash
./om daily-brief latest [--account lx] [--market US|HK] [--json]
./om daily-brief day --account lx [--market US|HK] --date YYYY-MM-DD [--revision N] [--json]
./om-agent run --tool daily_decision_brief_read --input-json '{}'
./om-agent run --tool daily_decision_brief_read --input-json '{"account":"lx","market":"US"}'
```

Delivery state inspection and migration remain explicit operator commands:

```bash
./om daily-brief delivery-inspect --account lx --market HK
./om daily-brief delivery-migrate --account lx --market HK          # dry-run
./om daily-brief delivery-migrate --account lx --market HK --confirm
```
