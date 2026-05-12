## Context

`./om run tick --accounts ...` uses one multi-account orchestration path for both
single-account and multi-account runs. The scheduler command supports
account-scoped decisions, but the orchestrator previously used the global
`should_run_scan` decision for every account.

## Decision

Multi-account tick orchestration must carry account-scoped scan decisions from
`run_scheduler_flow()` into `run_one_account()`.

Rules:

1. Global scheduler output remains the run-level fallback and audit snapshot.
2. Each account gets its own scheduler probe result in `scan_decision_by_account`.
3. `run_one_account()` prefers the account decision for `should_run` and `reason`.
4. Scheduler `mark_scanned` updates are written only for accounts that actually
   ran the pipeline.
5. `--default-account` must point at an active account for the current run.
