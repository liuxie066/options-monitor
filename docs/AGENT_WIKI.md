# Agent Wiki — options-monitor

> Agent-oriented codebase reference. For human-readable architecture overview, see `ARCHITECTURE.md`.

## 1. Module Map by Task

### Candidate Scanning
- **Domain engine**: `domain/domain/engine/candidate_engine.py`
  - Core function: `evaluate_candidates(candidates_df, constraints, context) → (passed_df, filtered_df)`
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
- **Projection logic**: `domain/domain/option_position_ledger.py`
  - Model: `trade_events → projection → position_lots`
- **Application services**: `src/application/option_positions_facade.py`, `src/application/option_positions_inspection.py`, and related SQLite/CLI wrappers
- **Rule**: Feishu `option_positions` is a bootstrap/mirror surface, not the steady-state source of truth. Local SQLite is the source of truth.

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
→ domain.domain.engine.candidate_engine.evaluate_candidates
```

### Option Position Flow
```text
trade_events
→ domain.domain.option_position_ledger.projection
→ position_lots
→ SQLite (source of truth)
→ optional Feishu mirror / bootstrap
```

## 4. Key Function Signatures

You do not need to read these files to understand what they do.

### `domain/domain/engine/candidate_engine.py`
```python
def evaluate_candidates(
    candidates_df: pd.DataFrame,
    constraints: dict,
    context: CandidateContext,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns: (passed_candidates, filtered_candidates).
    Steps: normalization → hard constraints → return floor → risk filter → ranking.
    """
```

### `domain/domain/option_position_ledger.py`
```python
def project_position_lots(
    trade_events: list[TradeEvent],
    as_of_date: date | None = None,
) -> list[PositionLot]:
    """
    Projects trade events into option position lots.
    """
```

### `domain/domain/close_advice.py`
```python
def generate_close_advice(
    positions: list[PositionLot],
    market_quotes: dict[str, Quote],
    policy: ClosePolicy,
) -> list[CloseAdvice]:
    """
    Deterministic close-advice decisions.
    """
```

### `src/application/multi_account_tick.py`
```python
def run_tick(
    config_path: str,
    accounts: list[str],
    run_id: str | None = None,
    dry_run: bool = False,
) -> TickResult:
    """
    Unified tick entry. Single account = ['lx']; multi = ['lx', 'sy'].
    """
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
