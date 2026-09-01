# Architecture

`options-monitor` is an operations-sensitive local application. The code is
organized around stable entry points, application use cases, deterministic
domain rules, external adapters, and local state repositories.

## Layers

| Layer | Path | Owns |
|---|---|---|
| Interfaces | `src/interfaces/` | Human CLI, Tool Gateway, and Inbound Assistant request/response adaptation |
| Application | `src/application/` | Use-case orchestration, config assembly, pipeline execution, notification flow |
| Domain | `domain/domain/` | Deterministic strategy, scheduler, notification, position, and schema decisions |
| Infrastructure | `src/infrastructure/` | OpenD/Futu, Feishu, WeChat ClawBot, exchange-rate and subprocess adapters |
| Storage | `domain/storage/` | Local path conventions and repository-style reads/writes for run state and reports |

Rules:

- `domain/domain` must not import `src` or `scripts`.
- `src/application` must not import `scripts`.
- `scripts/` is reserved for operational wrappers, release helpers, diagnostics,
  and one-off tools that delegate into `src` or `domain`.
- Public behavior should enter through `./om`, `./om-agent`, or
  documented `python -m src.application...` entry points.

## Main Entry Points

`./om` is the human CLI. It forwards to `src.interfaces.cli.main`.

`./om-agent` is the structured Tool Gateway CLI. It forwards to
`src.interfaces.agent.cli`, where tool execution is routed through:

```text
src.interfaces.agent.cli
-> src.application.tool_execution
-> src.application.agent_tool_registry
-> src.application.agent_tools.<domain>.TOOLS
```

`src.application.agent_tool_registry` is a collector/manifest adapter. New Tool
Gateway tools should live in `src/application/agent_tools/` domain modules and
export `TOOLS`. All Tool Gateway tools execute through `AgentTool.call(ctx,
payload)`. Root-level `src/application/agent_tool_*.py` modules are reserved
for compatibility re-exports and shared helpers such as config/contracts; they
must not own tool implementations. The legacy
`src.application.agent_tool_handlers` switchboard has been removed.

`./om copilot ...` is the local/eval entry for the read-only Copilot. Channel
Copilot is read-first and may request deterministic Control previews. Both are part
of the human CLI surface, not the Tool Gateway manifest:

```text
./om copilot run|eval
-> src.interfaces.cli.copilot_ops
-> src.application.copilot.local_harness
-> Service prepares an ExecutionContract
-> Host prepares a SceneManifest, runs the Agent/Engine loop, records events,
   and admits the final AppResult
```

The same Service, Host, Scene, Agent, and pure-read tool projection serve local
and channel questions. Channel adapters call the Copilot channel facade rather
than Host or Agent directly. There is one Scene, `om_chat`; no per-question
Scene selection or channel Scene allowlist exists. Host-backed channel runs
persist their session, run, and event lifecycle in the Copilot Host store and a
sanitized summary in the inbound audit record.

## Research, Shadow Replay, And Strategy Lab

Research and Shadow Replay are an independent offline evidence/replay module,
not part of the Inbound Assistant core and not a remote chat surface. Strategy
Lab is a separate strategy-experiment product surface. Its current executable
surface contains the fixed HK / `lx` Recipe, preview and confirmation, status,
explicit bounded 20-day research execution, Research Receipt reading, and targeted
history-K readiness. Hidden validation remains outside Phase 2.

```text
./om research ...
-> src.interfaces.cli.research
-> src.application.research
-> src.application.shadow_replay

./om strategy-lab readiness refresh-history-k ...
-> src.interfaces.cli.strategy_lab_ops
-> src.application.strategy_lab.service/readiness

./om strategy-lab recipes|preview|confirm-research|status|receipt ...
./om strategy-lab research execute ...
-> src.interfaces.cli.strategy_lab_ops
-> src.application.strategy_lab.service
-> src.application.strategy_lab.recipe/evidence/comparison/receipts
```

`src.application.research` owns redacted evidence collection, deterministic
checks, handoff rendering, remote archive mirroring, and local research bundle
writes. `src.application.shadow_replay` owns offline dataset construction,
mark/outcome lifecycle, status, analyze, and candidate-impact report logic.
`src.application.strategy_lab` owns the root experiment contract, Recipe preview,
bounded research evidence, comparison, Research Receipt, and targeted readiness; the
same-path `ExperimentStore` is the three-table state owner.
Shadow Replay maintenance stays on its own commands. Strategy Lab does not own
a recorder service or timer in Phase 2. Provider evidence is admitted only after the
non-blocking query lock and Tick/protection checks; one invocation consumes at most
one provider logical unit.

This side lane may read runtime artifacts, candidate/reject/trace evidence,
required-data snapshots, and archived run outputs. It must not mutate runtime
config, notification behavior, Feishu/ledger/trade state, broker-facing data,
or live tick scheduling. Research has no universal write flag. `collect` writes
only with `--write-outputs --confirm`; maintenance and archive pull/build
commonly use `--write`; `shadow-replay build`,
`shadow-replay candidate-impact-report`, `archive verify`, and commands given
explicit output paths materialize local artifacts as part of their own
contract. `archive prune-remote` is a separate destructive boundary and
deletes remote runtime artifacts only with `--confirm` after local
verification. Inspect the selected `./om research <subcommand> --help` before
execution.

Product boundary:

```text
Research = evidence infrastructure
Shadow Replay = counterfactual replay engine
Strategy Lab = strategy evolution product surface
```

## Inbound Flow

Remote messages intentionally separate channel transport from application
execution. The current CLI namespace remains `./om assistant ...`:

```text
Feishu / future channels
-> src.application.channels.ChannelService
-> src.application.inbound.feishu(_ws)
-> sender allowlist / idempotency
-> explicit command or pending-operation reply?
   -> deterministic Control
   otherwise -> Copilot Service -> Host -> om_chat Agent
-> audit / operation persistence
-> channel reply
```

`src.application.channels` owns channel capability registration and dispatch, so
Inbound control and outbound notifications are capabilities of the
same channel model. `src.application.inbound` should stay thin: extract channel
payloads, enforce channel-specific receive/reply mechanics, and build the
transport request.
`AssistantRequest`, the inbound audit row, and the pending-operation store form
the deterministic Control boundary. Protocol command parsing, bound permission
responses, sender allowlist checks, previews, confirmations, applies, and
operation receipts are owned by `src.application.assistant`.

Every message that is not explicit Control protocol enters the read-first
Copilot path. Copilot Service prepares the execution contract, Host owns
session/run/event governance and the `om_chat` Scene, and Agent/Engine own the
generic model/tool loop. The model can use canonical pure-read tools and, on
channel runs, one generic Control-preview meta-tool. It cannot receive or call
confirmation, cancellation, apply, notification, config-write, ledger/trade,
broker-write, service-control, or upgrade handlers directly. The deterministic
Inbound service validates a preview request against the capability catalog
before Control creates the pending operation.

Control receipts are stored as conversation context for follow-ups. Each turn
also receives a fresh current-conversation pending snapshot from the operation
store, so stale or compacted chat history cannot become operation authority.

There is no business intent router, multi-Scene catalog, planner fallback,
evidence pipeline, or synthetic Assistant Agent session. Missing model
configuration or unavailable evidence produces an explicit Copilot failure; it
does not fall back to ordinary chat or deterministic business templates.

Model selection is a startup/configuration concern. `config.yaml` may define multiple
`assistant.models` profiles and an `assistant.active_model`, but
`config build-assistant` resolves that into one flat `assistant.llm` in
`config.assistant.json`. Control and tool execution do not choose models per
message.

Inbound uses one explicit command/permission contract. Slash commands never call
the model. Bound confirm/cancel phrases such as `确认升级` enter the permission
path only when they match an existing pending operation in the same
sender/channel/conversation scope. All other text enters Copilot. Deterministic
code must not recover natural-language business intent through unrelated command
fallback.
Any preview-write result can only enter an existing pending-operation path.
Confirm, cancel, apply, notifications, direct config writes, ledger/trade
writes, and service operations remain outside model authority.

Control emits one `ControlExecution`; Copilot emits one Host-owned `AppResult`.
Neither path recreates perception/reasoning/action/observation stages.

## Runtime Tick Flow

The live scan/notification flow has one public chain:

```text
./om run tick --config <runtime-config.json>
-> src.interfaces.cli.main
-> src.application.multi_account_tick.run_tick
-> src.application.multi_account_tick.main
```

Inside the tick use case:

```text
multi_account_tick
-> config contract + run/idempotency context
-> OpenD watchdog / project guard / trading-day guard
-> scheduler decision
-> account execution
-> notification preparation and delivery
-> final run state and audit writes
```

`multi_account_tick` should stay as the orchestration spine. Narrow helper
modules own the heavier subflows:

- `src.application.tick_run_context`: tick idempotency bucket/key construction
  and completion record writes.
- `src.application.tick_guard_flow`: project guard, load shedding, market-scoped
  config filtering, OpenD phone-verify gate, and watchdog admission.
- `src.application.tick_run_workspace`: run directory, required-data workspace,
  and shared state pointer.
- `src.application.tick_scheduler_context`: trading-day guard, scheduler state
  path, scheduler decision, and scheduler snapshot writes.
- `src.application.tick_account_execution`: account defaults, account worker
  limits, ordered concurrent account execution, per-account metrics, and
  scan-state marking.
- `src.application.tick_notification_flow`: notification preparation, quiet-hour
  decision, delivery, metrics, finalization, and notification idempotency
  completion.

Account execution is per account:

```text
account_run.run_one_account
-> required_data prefetch
-> event prefetch / event_snapshot.json
-> pipeline_runtime / pipeline_watchlist / pipeline_symbol
-> optional close advice
-> account metrics and account notification text
```

Expired-position maintenance is a separate
`option-positions auto-close-expired` service/timer workflow. It is not a stage
of the live tick account flow.

## Scan And Candidate Flow

Candidate scanning is intentionally split:

```text
src.application.pipeline_runtime
-> src.application.pipeline_watchlist
-> src.application.pipeline_symbol
-> src.application.scan_sell_put / scan_sell_call
-> src.application.candidate_scanning
-> domain.domain.engine.candidate_engine
```

The canonical candidate decisions live in `domain.domain.engine.candidate_engine`:

- input normalization
- hard constraints
- return floor
- risk filter
- ranking

Application scanners adapt files, pandas rows, context, and report output around
that domain engine. Avoid adding parallel ranking implementations in adapters.

Event-risk data is prepared at run scope, not inside candidate scanning:

```text
src.application.events.prefetch
-> src.application.events.store
-> output_runs/<run_id>/state/event_snapshot.json
-> src.application.events.annotator
-> domain.domain.short_vol_assessment
```

The candidate scan path must only read the run snapshot. It must not call the
external event source directly; source failures remain explicit as `error` or
`stale`, and short-vol fail-closed policy is enforced in the domain assessment.

Strategy terminology is centralized in `domain.domain.strategy_vocab`.
Application and interface code should use it to translate between stable
internal ids such as `sell_call` and user-facing names such as `Covered Call (CC)`.
Do not scatter display names, aliases, or section labels through notification,
report, or Tool Gateway manifest code.

## Option Positions Flow

The durable position model is:

```text
trade_events -> projection -> position_lots
```

Domain projection logic lives in `domain.domain.ledger.projection`.
Stored trade events are encoded at `src.application.ledger.event_codec`; runtime
writes use the canonical ledger event schema.
Lot record field construction and open/close patch helpers live under
`domain.domain.ledger.position_fields`.
Application services own SQLite loading, local bootstrap, repair, CLI facades,
and reports. Feishu `option_positions` is not a bootstrap input, sync target,
strategy input, or steady-state source of truth.
`src.application.ledger.api` is the public application boundary for all
non-ledger runtime code. `src.application.positions`, `src.application.trades`,
agent tools, CLI modules, web UI modules, pipeline context, and cash-headroom
queries must not import ledger internals such as service, preflight, resolver,
writer, publisher, repository, projection-verify, or read-model modules directly.
The API file is intentionally a thin facade: command/write operations live in
`src.application.ledger.commands`, query/read operations live in
`src.application.ledger.queries`, and typed read views such as
`PositionLotSnapshot` and `RiskPositionView` live in
`src.application.ledger.views`.
Runtime callers should call semantic ledger actions such as manual position
recording, broker trade recording, expired-close planning/recording, projection
refresh, lot selection, position snapshot reads, event review/repair, and
projection verification, rather than composing lower-level
`persist_*`, `preflight_*`, `require_*`, or `load_*` functions themselves.
Close writes share `CloseTargetResolution` as the ledger-owned target contract:
manual close resolves a unique strict lot, broker close resolves a strict exact
FIFO target set, and auto-close validates the explicit current lot before
writing. Aggregated `position_key` values are read-only and must not become
write targets.

Position-facing workflows live under `src.application.positions`; they operate
on projected lots and expose manual lot operations, expiry maintenance, and
maintenance receipts, plus risk context, inspection, and reporting.
Trade-facing workflows live under `src.application.trades`; they
operate on normalized trade deals, OpenD deal intake, idempotency state,
receipts, and event review/replay flows. Both route writes through
`src.application.ledger` instead of owning projection or matching rules locally.

For a first-seen Futu stock or ETF deal, trade intake may emit a post-settlement
account refresh hint to the separately installed portfolio-management service.
The hint contains no position delta: PM owns the full Futu holdings reread,
persistence, scheduling, concurrency, and retry policy. OM only records whether
the hint request was accepted or failed; acceptance is not synchronization
evidence. The root `portfolio_management.enabled` switch gates this hint and all
other production PM callers.

## Close Advice Flow

Close advice keeps deterministic policy in `domain.domain.close_advice`.
`src.application.close_advice_runner` assembles option-position inputs, required
data quotes, quality flags, Futu fee estimates, rows, and output files around
that domain logic.
Domain policy owns one fixed `strict_profit_capture.v1` rule for short puts and
short calls. It emits only `close`, `hold`, or `not_evaluable`; it does not pair
combo-yield legs, compare opening candidates, or produce roll,
replacement, reallocation, short-vol, or long-option exit actions. The runner
preserves those strict decisions, including fail-closed `not_evaluable` rows,
and publishes CSV/text reports plus their integrity manifest. The Close Advice
state and evidence contract is documented in
`docs/CLOSE_ADVICE_CONTRACT.md`.

## Config And Runtime State

The canonical human authoring source is `config.yaml`. Code-owned defaults live
in `src.application.config_defaults.DEFAULT_CONFIG`, and YAML overrides are
resolved by `src.application.config_yaml`.

Market runtime configs are generated snapshots:

- `config.us.json`
- `config.hk.json`

Runtime execution consumes those JSON snapshots rather than editing
`config.yaml` directly. First-run setup uses `src.application.config_yaml_init`
to create a starter YAML file and build market snapshots. Legacy JSON authoring
and its migration command are retired. Production upgrade requires a usable YAML
authoring source and fails closed before switching releases when it is absent.

Shared config section helpers such as symbol/watchlist and templates live in
`src.application.config_sections`; both loading and validation depend on that
neutral module to avoid loader/validator cycles.

Runtime state is local and intentionally explicit:

- shared state: `output_shared/state/`
- per-account output: `output_accounts/<account>/`
- run snapshots: `output_runs/<run_id>/`
- cache files: `cache/`

## Change Guidance

When adding code:

- Put pure business decisions in `domain/domain`.
- Put use-case orchestration in `src/application`.
- Put external system adapters in `src/infrastructure`.
- Put CLI, Tool Gateway, and Inbound Assistant argument/response adaptation in `src/interfaces`.
- Prefer a small facade-preserving move over changing public command behavior.
- Add or update boundary tests when moving ownership between layers.
