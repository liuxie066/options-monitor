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

## Research, Shadow Replay, And Strategy Lab

Research and Shadow Replay are an independent offline evidence/replay module,
not part of the Inbound Assistant core and not a remote chat surface. Strategy Lab is
the strategy-evolution product surface above that offline module; its current
implemented surfaces are evidence update, read-only decision-instance
readiness, experiments, advisory proposals, and redacted local LLM context.

```text
./om research ...
-> src.interfaces.cli.research
-> src.application.research
-> src.application.shadow_replay
-> src.application.strategy_lab
```

`src.application.research` owns redacted evidence collection, deterministic
checks, handoff rendering, remote archive mirroring, and local research bundle
writes. `src.application.shadow_replay` owns offline dataset construction,
mark/outcome lifecycle, status, analyze, and candidate-impact report logic.
`src.application.strategy_lab` currently owns dataset evidence loading,
decision-instance normalization, domain adapters, controlled hypothesis
generation, Strategy Lab readiness, experiment workflow, and observed-universe
scorecards while reusing Shadow Replay for counterfactual evaluation. It also
owns advisory proposal generation and redacted LLM context generation.
Its update facade wraps Shadow Replay status / data-plan so dataset mark and
settle maintenance has one Strategy Lab entry point without adding a new write
path.
The shared core owns the workflow only. Sell Put, Covered Call, and Combo
Yield keep separate decision units, objective metrics, tunable parameters,
hard constraints, and proposal targets inside strategy-domain adapters.
Combo Yield is group-level only and must not be optimized through the
single-leg candidate-impact parameter model.

This side lane may read runtime artifacts, candidate/reject/trace evidence,
required-data snapshots, and archived run outputs. It must not mutate runtime
config, notification behavior, Feishu/ledger/trade state, broker-facing data,
or live tick scheduling. Writes are limited to local research/replay artifacts
behind explicit `./om research ... --write` or `--write-outputs --confirm`
flags.

Product boundary:

```text
Research = evidence infrastructure
Shadow Replay = counterfactual replay engine
Strategy Lab = strategy evolution product surface
```

## Inbound Flow

Remote chat control intentionally separates channel transport from Inbound
control. The current CLI namespace remains `./om assistant ...`, and the
current implementation path remains `src/application/assistant/...`:

```text
Feishu / future channels
-> src.application.channels.ChannelService
-> src.application.inbound.feishu(_ws)
-> AgentSession / AgentSessionSnapshot
-> AgentLoop
   -> Perceive
   -> Understand
   -> Plan
   -> Decide
   -> Act
   -> Observe
   -> Verify / Replan
   -> Compose / Verify Answer
-> channel reply
```

`src.application.channels` owns channel capability registration and dispatch, so
Inbound control and outbound notifications are capabilities of the
same channel model. `src.application.inbound` should stay thin: extract channel
payloads, enforce channel-specific receive/reply mechanics, and build the
transport request.
`AgentSession` is the Agent task boundary, currently represented by
`AssistantRequest`, `AgentSessionSnapshot`, the audit row, conversation context,
pending-operation store, and durable operator trace in the inbound SQLite
`agent_sessions` table. It is not a separate runtime service or a second
pending-operation store.
Protocol command parsing, bound permission responses, natural-language
AgentLoop routing, sender allowlist checks, SQLite audit, preview/confirm
operations, and user-facing rendering are owned by
`src.application.assistant`.

The current implementation path still contains files named `runtime.py`,
`router.py`, `perception.py`, `reasoning.py`, `action.py`, and
`observation.py`; those are implementation handles inside one Agent loop, not
separate product runtime layers. `assistant.mode` is retired and unsupported;
the active product path is controlled by `assistant.enabled` and
`assistant.agent_loop.enabled`. `assistant.planner.enabled` is a deprecated
compatibility alias only.

LLM providers are optional. In the default `agent_loop`, OM Copilot derives the
task frame, evidence plan, preview route, and answer from deterministic OM
tools. Provider model-turn planning remains an explicit injected/diagnostic
compatibility path, not the default free-form route. Deterministic OM tools own
facts, and write actions remain behind preview/confirm gates.

Model selection is a control-plane concern. `config.yaml` may define multiple
`assistant.models` profiles and an `assistant.active_model`, but
`config build-assistant` resolves that into one flat `assistant.llm` in
`config.assistant.json`. Runtime, router, perception, reasoning, action, and
tool execution must not depend on model profiles or choose models per message.

Inbound uses one internal Agent-loop contract. Slash commands enter through the
ProtocolGate command parser and never call LLM. Bound confirm/cancel phrases
such as `确认升级` enter PermissionResponseGate only when they match an
existing pending operation in the same sender/channel/conversation scope.
All other non-slash natural language enters AgentLoop / OM Copilot.
Deterministic code is limited to protocol parsing, permission binding, task
profiles, evidence planning, tool execution, validators, renderers, and
operation apply boundaries; it must not recover natural-language business
intent through unrelated command fallback. Objective slot repair is handled by
small focused helpers such as month filters or position-query filters, not by a
generic natural-language parser.
AgentLoop may plan read tools or exactly one preview capability from
`manual_trade_open`, `manual_trade_close`, `manual_assignment`, `manual_expiry`,
`manual_trade_update`, `symbol_edit`, `model_use`, or `upgrade_now`. Any preview-write result can only enter an
existing pending-operation path. Confirm, cancel, apply, notifications, direct
config writes, ledger/trade writes, and service operations remain outside model
authority.

```text
Perceive   -> AssistantRequest / channel context
Understand -> PerceptionResult or internal Copilot event plan
Decide     -> ReasoningResolution / permission decision
Act        -> ActionResult / ToolResult / PendingOperation
Observe    -> ObservationResponse / audited reply
```

ProtocolGate, PermissionResponseGate, and AgentLoop must only emit
`PerceptionResult` or bounded Copilot task/evidence events. Tool-name
selection, config-scoped payload construction, capability support, safety class
validation, and confirmation requirements are owned by
`src.application.assistant.reasoning`, Copilot task profiles, and AgentLoop
validation. Execution is owned by `src.application.assistant.action`, and
response shaping is owned by `src.application.assistant.observation`.

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
-> expired position maintenance
-> required_data prefetch
-> event prefetch / event_snapshot.json
-> pipeline_runtime / pipeline_watchlist / pipeline_symbol
-> optional close advice
-> account metrics and account notification text
```

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
internal ids such as `sell_call` and user-facing names such as `Covered Call`.
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

## Close Advice Flow

Close advice keeps deterministic policy in `domain.domain.close_advice`.
`src.application.close_advice_runner` assembles option-position inputs, required
data quotes, quality flags, fees, yield-enhancement leg pairing, rows, and
output files around that domain logic.
Domain policy owns return capture, short-vol risk exits, long-call convexity
exits, and the exit-state contract. The runner preserves domain decisions,
including `not_evaluable` rows, and maps them through the close-action policy
registry to CSV/text actions such as `close_put_keep_call`,
`hold_call_as_convexity`, and `close_both_optional`.
The close-advice exit-state contract and scenario matrix are documented in
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
to create a starter YAML file and build market snapshots. Legacy JSON overlays
under `configs/` are migration/upgrade-recovery inputs only, not a normal
authoring path.

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
