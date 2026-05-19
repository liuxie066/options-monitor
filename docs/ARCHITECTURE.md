# Architecture

`options-monitor` is an operations-sensitive local application. The code is
organized around stable entry points, application use cases, deterministic
domain rules, external adapters, and local state repositories.

## Layers

| Layer | Path | Owns |
|---|---|---|
| Interfaces | `src/interfaces/` | Human CLI and Agent CLI request/response adaptation |
| Application | `src/application/` | Use-case orchestration, config assembly, pipeline execution, notification flow |
| Domain | `domain/domain/` | Deterministic strategy, scheduler, notification, position, and schema decisions |
| Infrastructure | `src/infrastructure/` | OpenD/Futu, Feishu, OpenClaw, exchange-rate and subprocess adapters |
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

`./om-agent` is the structured Agent CLI. It forwards to
`src.interfaces.agent.cli`, where tool execution is routed through:

```text
src.interfaces.agent.cli
-> src.application.tool_execution
-> src.application.agent_tool_registry
-> src.application.agent_tool_handlers
```

## Runtime Tick Flow

The live scan/notification flow has one public chain:

```text
./om run tick
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
  shared state pointer, and legacy `output` link preparation.
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

## Option Positions Flow

The durable position model is:

```text
trade_events -> projection -> position_lots
```

Domain projection logic lives in `domain.domain.ledger.projection`; the old
`domain.domain.option_position_ledger` projection module is retired.
Stored trade events are encoded at `src.application.ledger.event_codec`, so new
writes use the canonical ledger event schema while old rows are decoded only at
the storage/migration boundary.
Lot record field construction and open/close patch helpers live under
`domain.domain.ledger.position_fields`; the old
`domain.domain.option_position_lots` module is only a compatibility re-export.
Application services own SQLite loading, local bootstrap, repair, CLI facades,
and reports. Feishu `option_positions` is retired: it is not a
bootstrap input, sync target, strategy input, or steady-state source of truth.
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
data quotes, quality flags, fees, rows, and output files around that domain
logic. Future optimizer work should keep new scoring policy in domain and keep
runner modules focused on input/output orchestration.

## Config And Runtime State

Canonical runtime configs are:

- `config.us.json`
- `config.hk.json`

Layered config source files live under `configs/` and are built by
`src.application.layered_config`.

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
- Put CLI and Agent CLI argument/response adaptation in `src/interfaces`.
- Prefer a small facade-preserving move over changing public command behavior.
- Add or update boundary tests when moving ownership between layers.
