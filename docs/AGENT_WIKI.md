# Agent Wiki — options-monitor

> Agent-oriented codebase reference. For human-readable architecture overview, see `docs/ARCHITECTURE.md`.

## 1. Module Map by Task

### Candidate Scanning
- **Domain engine**: `domain/domain/engine/candidate_engine.py`
  - Core functions: `evaluate_candidate_input`, `evaluate_candidate_hard_constraints`,
    `evaluate_candidate_return_floor`, `evaluate_candidate_risk_filter`,
    `rank_candidate_rows`
  - Steps: input normalization → hard constraints → return floor → risk filter → ranking
- **Application adapters**: `src/application/candidate_scanning.py`, `src/application/scan_sell_put.py`, `src/application/scan_sell_call.py`
- **Rule**: Do not add parallel ranking logic in application adapters.

### Notification Formatting
- **Per-account content**: `src/application/notify_symbols.py`
- **Account wrapper / multi-account format**: `src/application/multi_tick/notify_format.py`
- **Tests**: `tests/test_notify_symbols_markdown.py`, `tests/test_multi_tick_notify_format.py`

### Close Advice
- **Domain policy**: `domain/domain/close_advice.py`
- **Runner (I/O orchestration)**: `src/application/close_advice_runner.py`
- **Rule**: Keep new scoring policy in domain; runner stays focused on input/output assembly.

### Option Positions
- **Projection logic**: `domain/domain/ledger/projection.py`
  - Model: `trade_events → projection → position_lots`
- **Public application API**: `src/application/ledger/api.py`
  - The only ledger import surface for `src/application/positions/` and `src/application/trades/`.
  - Core workflows use semantic actions (`record_manual_position_*`, `record_broker_trade_*`,
    `plan_expired_position_closes`, `record_expired_position_closes`) instead of composing
    lower-level `persist_*` / `preflight_*` primitives directly.
- **Canonical use-case service**: `src/application/ledger/service.py`
- **Repository/config boundary**: `src/application/ledger/repository.py`
- **Stored event codec**: `src/application/ledger/event_codec.py`
- **Bootstrap/migration materialization**: `src/application/ledger/bootstrap.py`
- **Event write + projection publish**: `src/application/ledger/writer.py`
- **Manual open/close/adjust writes**: `src/application/ledger/manual_trades.py`
- **Manual void/repair interventions**: `src/application/ledger/interventions.py`
- **Auto-close maintenance**: `src/application/ledger/maintenance.py`
- **Ledger preflight checks**: `src/application/ledger/preflight.py`
- **Close lot resolver**: `src/application/ledger/lot_resolver.py`
- **Lot target identity guard**: `src/application/ledger/targets.py`
- **Canonical read model**: `src/application/ledger/read_model.py`
- **Position-facing workflows**: `src/application/positions/workflows.py`
  - Manual lot open/close/adjust, post-write Feishu mirror sync, and strict close-target resolution.
- **Position maintenance entrypoints**: `src/application/positions/auto_close.py`, `src/application/positions/maintenance.py`, `src/application/positions/maintenance_receipt.py`
  - CLI auto-close orchestration, per-account expiry maintenance, and maintenance receipts.
- **Position mirror/sync entrypoints**: `src/application/positions/feishu_sync.py`, `src/application/positions/feishu_sync_receipt.py`, `src/application/positions/sync_config.py`
  - Feishu mirror sync, sync receipt policy, and runtime sync enablement.
- **Position read/report entrypoints**: `src/application/positions/context_builder.py`, `src/application/positions/inspection.py`, `src/application/positions/reporting.py`
  - Risk context, projection inspection, lot event history, and income reporting.
- **Trade-facing workflows**: `src/application/trades/workflows.py`, `src/application/trades/review.py`
  - Normalized trade-deal open/close application, event review, replay, void, and repair.
- **Trade intake entrypoints**: `src/application/trades/auto_intake.py`, `src/application/trades/resolver.py`, `src/application/trades/normalizer.py`
  - OpenD deal push intake, account mapping, normalization, idempotency state, receipts, and lot resolution.
- **Application boundary**: `src/application/ledger/` owns storage/projection/preflight, `src/application/positions/` owns position-facing workflows, and `src/application/trades/` owns trade-facing workflows. `positions` and `trades` must enter ledger through `src/application/ledger/api.py`, not internal ledger modules.
- **Rule**: Feishu `option_positions` is an explicit bootstrap input or mirror surface, not the steady-state source of truth. Local SQLite `trade_events` is the source of truth.

### Tick Runtime
- **Orchestration spine**: `src/application/multi_account_tick.py`
- **Helper modules**:
  - `tick_run_context` — idempotency bucket/key construction and completion records
  - `tick_guard_flow` — project guard, load shedding, market filtering, OpenD phone-verify gate, watchdog admission
  - `tick_run_workspace` — run directory, required-data workspace, shared state pointer
  - `tick_scheduler_context` — trading-day guard, scheduler state path, scheduler decision
  - `tick_account_execution` — account defaults, worker limits, ordered concurrent execution, per-account metrics
  - `tick_notification_flow` — notification prep, quiet-hour decision, delivery, metrics, idempotency completion

### Configuration
- **Layered config build**: `src/application/layered_config.py`
- **Runtime validation**: `src/application/config_validator.py`
- **Templates**: `configs/examples/user.example.us.json`, `configs/examples/user.example.hk.json`

### Agent Tooling
- **Manifest**: `src/application/agent_tool_registry.py`
- **Handlers**: `src/application/agent_tool_handlers.py`
- **Contracts**: `src/application/agent_tool_contracts.py`
- **Config helpers**: `src/application/agent_tool_config.py`, `src/application/agent_tool_init_local.py`
- **CLI**: `src/interfaces/agent/cli.py` → `./om-agent`

### Human / Operator CLI
- **CLI main**: `src/interfaces/cli/main.py` → `./om`

## 2. Import Constraints

```text
domain/domain/        → MUST NOT import src/, scripts/
src/application/      → MUST NOT import scripts/
src/infrastructure/   → May import src/application/, domain/
src/interfaces/       → May import all upper layers
scripts/              → Operational wrappers only; delegate to src/ or domain/
```

## 3. Key Data Flows

### Tick Flow
```text
./om run tick
→ src.application.multi_account_tick.run_tick
  → tick_guard_flow (OpenD watchdog, project guard, trading-day guard)
  → tick_scheduler_context (scheduler decision)
  → tick_account_execution
    → expired position maintenance
    → required_data prefetch
    → pipeline_runtime / pipeline_watchlist / pipeline_symbol
    → optional close advice
    → account metrics + per-account notification text
  → tick_notification_flow (quiet-hour, delivery, finalization)
  → run state + audit writes
```

### Candidate Scan Flow
```text
src.application.pipeline_runtime
→ pipeline_watchlist
→ pipeline_symbol
→ scan_sell_put / scan_sell_call
→ candidate_scanning
→ domain.domain.engine.candidate_engine
  → evaluate_candidate_input / evaluate_candidate_hard_constraints
  → evaluate_candidate_return_floor / evaluate_candidate_risk_filter
  → rank_candidate_rows
```

### Option Position Flow
```text
trade_events
→ domain.domain.ledger.projection
→ position_lots
→ SQLite projection
→ optional Feishu mirror / explicit bootstrap
```

Lot record field helpers live in `domain.domain.ledger.position_fields`.
`domain.domain.option_position_lots` is a compatibility re-export and should not
be used by new ledger / positions / trades code.

`src.application.ledger.api` is the only ledger boundary for non-ledger runtime
code. Application workflows, agent tools, CLI modules, web UI code, scan
context builders, and cash-headroom queries should import semantic actions from
that module only. Do not import ledger service / preflight / repository /
publisher / read-model modules outside `src.application.ledger`.

## 4. Key Function Signatures

You do not need to read these files to understand what they do.

### `domain/domain/engine/candidate_engine.py`
```python
def evaluate_candidate_input(row: dict[str, Any]) -> dict[str, Any]: ...
def evaluate_candidate_hard_constraints(payload: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]: ...
def evaluate_candidate_return_floor(payload: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]: ...
def evaluate_candidate_risk_filter(payload: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]: ...
def rank_candidate_rows(rows: list[dict[str, Any]], *, mode: StrategyMode | str) -> list[dict[str, Any]]: ...
```

### `domain/domain/ledger/projection.py`
```python
def project_trade_events(events: list[TradeEvent]) -> ProjectionResult: ...
def build_risk_position_views(lots: list[PositionLot]) -> list[RiskPositionView]: ...
```

### `domain/domain/close_advice.py`
```python
def evaluate_close_advice(inp: CloseAdviceInput, config: CloseAdviceConfig | None = None) -> dict[str, Any]: ...
def evaluate_close_optimizer(
    inp: CloseAdviceInput,
    optimizer_cfg: CloseOptimizerConfig,
    *,
    alternative_annualized_return: float | None = None,
) -> dict[str, Any]: ...
```

### `src/application/multi_account_tick.py`
```python
def run_tick(argv: list[str] | None = None) -> int: ...
```

## 5. Runtime State Paths

| Type | Path |
|---|---|
| Shared state | `output_shared/state/` |
| Per-account output | `output_accounts/<account>/` |
| Run snapshots | `output_runs/<run_id>/` |
| Cache (OpenD) | `cache/opend_option_chain/`, `cache/opend_option_expirations/` |
| Audit logs | `audit/run_logs/` |

## 6. Change Guidance

When adding or modifying code:

1. Pure business decisions → `domain/domain/`
2. Use-case orchestration → `src/application/`
3. External system adapters → `src/infrastructure/`
4. CLI/WebUI argument/response adaptation → `src/interfaces/`
5. Prefer a small facade-preserving move over changing public command behavior
6. Add or update boundary tests when moving ownership between layers
