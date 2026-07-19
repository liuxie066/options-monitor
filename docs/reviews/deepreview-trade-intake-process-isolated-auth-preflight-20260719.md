# Aggregate Deep Review — trade-intake-process-isolated-auth-preflight

- Gate: aggregate deepreview / re-review
- Decision: pass
- Base: `origin/main` (`1203c59d`)
- Reviewed head: `ed3b13fe`

## Findings

No accepted blocking or non-blocking findings remain after the slice review fix.

Previously accepted finding:

- CR-1 generated dependency graph stale — 已修复; graph is current at 469 production modules and zero cycles.

## Review lenses

### Correctness and lifecycle

The implementation addresses the observed failure at its owning boundary. The existing application already reaches blocked status and computes code 78; only interpreter shutdown is stuck on SDK-created non-daemon threads. The dedicated child contains those threads, and `hard_exit(78)` terminates that process without asking Python to join them. The parent has no Futu context and can return the observed child status normally.

### State-machine integrity

The existing status writer remains the sole owner of `blocked/auth_required/OPEND_NEEDS_PHONE_VERIFY`. The supervisor neither interprets logs nor writes status. The terminal chain is now deterministic:

```text
auth warning -> blocked artifact -> application 78 -> child hard exit 78
-> parent 78 -> systemd RestartPreventExitStatus=78
```

No terminal state can fall back into application retry through the supervisor.

### Concurrency and cancellation

- SDK threads are isolated to one disposable child.
- Multi-source coordination remains unchanged inside that child.
- Parent interruption terminates and joins the child, with kill fallback.
- A signal-terminated child cannot be reported as success.
- There is no constructor retry, extra context, or IPC for trade data.

### Architecture and coupling

The change adds one narrow application process adapter and one CLI delegation change. Domain, ledger, receipts, notifications, config, service rendering, and Futu adapter contracts are untouched. The child calls application `main` directly, preventing recursive CLI spawning.

### Security and operational safety

No secrets, permissions, service configuration, notification behavior, broker writes, or ledger semantics changed. Hard exit is restricted to the trade-intake child after application return and explicit stream flush.

### Validation evidence

```text
2708 passed, 10 skipped — full repository
284 passed — trades + service deployment regression
105 passed — process supervisor + service/CLI delegation
12 passed — push listener + restart policy
smoke OK
ruff focused checks passed
compileall passed
dependency graph current — 469 modules, 0 cycles
git diff --check passed
```

The real subprocess regression creates a non-daemon 60-second thread and proves exit 78 completes within a five-second timeout.

## Residual risks

- Production Futu/systemd behavior: assigned to the approved single production canary after merge/release.
- Child finalizers are skipped: accepted and contained; application state writes and output flush precede hard exit.
- Sudden SIGKILL cannot persist a terminal status: pre-existing operational limitation, visible through systemd.

All residual risks are classified. No blocking open question remains.
