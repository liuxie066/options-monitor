# Tick Orchestration Helper Boundaries

Decision: keep `src.application.multi_account_tick` as the public orchestration
spine, but move heavy subflows into narrow application helpers.

- `tick_run_context` owns tick idempotency key construction and completion writes.
- `tick_guard_flow` owns project guard, load shedding, market filtering,
  OpenD phone-verify gating, and watchdog admission.
- `tick_run_workspace` owns run directory, required-data workspace, shared state
  pointer, and legacy `output` link preparation.
- `tick_scheduler_context` owns trading-day guard, scheduler state path,
  scheduler decision, and scheduler snapshot writes.
- `tick_account_execution` owns account defaults, account worker limits, ordered
  concurrent execution, per-account metrics, and scan-state marking.
- `tick_notification_flow` owns notification preparation, delivery, metrics, and
  finalization.

Rationale: this preserves the `./om run tick` public chain while reducing the
size and responsibility of the main entry module.
