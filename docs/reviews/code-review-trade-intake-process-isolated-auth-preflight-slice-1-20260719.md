# Code Review — trade-intake-process-isolated-auth-preflight / slice 1

- Gate: code review / re-review
- Decision: pass
- Scope: process supervisor, CLI delegation, focused tests

## Findings

### CR-1 — Low — generated dependency graph was stale

Adding the supervisor production module changed the graph inventory from 468 to 469 modules. Initial `--check` failed. The generated graph was refreshed and rechecked at 469 modules with zero cycles.

Status: 已修复.

No other accepted correctness, stability, security, or maintainability findings remain.

## Evidence-based review

### Process lifecycle

- The hard exit occurs only in the spawned trade-intake child, after `auto_intake.main()` has returned and after stdout/stderr flush.
- The CLI parent never imports or constructs Futu contexts; SDK-created threads are confined to the disposable child.
- Exit 78 and ordinary non-zero codes are propagated unchanged.
- Negative multiprocessing exit codes are converted to conventional non-zero shell statuses; missing exit status fails closed.

### State machine and recovery

- Existing auth path remains `warning -> blocked status write -> return 78`.
- New lifecycle appends `child hard exit 78 -> parent return 78`; it does not add a second auth classifier or status writer.
- Parent KeyboardInterrupt terminates and joins the child; no retry or orphan path was introduced.

### Architecture / coupling

- Process ownership stays in the application/CLI boundary.
- Domain, ledger, notification, receipt, config, and service-unit contracts are unchanged.
- Wrapping the complete application avoids unsafe SDK context transfer and avoids per-source IPC.

### Test quality

- The subprocess regression uses a real non-daemon thread and a five-second timeout, reproducing the exact interpreter-shutdown failure class.
- Unit tests cover code 78, signal mapping, missing status, child application-code forwarding, and interruption cleanup.
- Existing CLI forwarding tests now assert the supervisor boundary without weakening argument assertions.

## Residual risks / uncovered areas

- Real Futu SDK can only be proven in production: covered by the approved single canary after release.
- systemd control-group signal behavior is platform/runtime evidence rather than a unit-test concern: covered by existing generated unit defaults and rollout observation.

All residual risks are classified; no blocking open question remains.
