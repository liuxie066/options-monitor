# Gateflow Plan — trade-intake-process-isolated-auth-preflight

- Gate: plan
- Base: `origin/main` at v1.2.417 (`1203c59d`)
- Production evidence: v1.2.417 detects `OPEND_NEEDS_PHONE_VERIFY`, but the process remains alive and the SDK continues its six-second reconnect log loop.
- Status: proposed

## Goal / motivation / success signal

Make the `./om run trade-intake` process boundary deterministic when the Futu SDK leaves non-daemon transport threads behind. The application child must persist the existing blocked status, terminate with exit code 78 without waiting for SDK threads, and let the supervising CLI process return 78 so systemd applies `RestartPreventExitStatus=78`.

Success requires:

1. auth detection still writes `status=blocked`, `stage=auth_required`, and `error_code=OPEND_NEEDS_PHONE_VERIFY`;
2. the listener execution process exits even when a non-daemon thread remains alive;
3. the CLI supervisor returns the child's terminal code unchanged, including 78;
4. normal listener operation remains long-running and systemd stop remains bounded;
5. production canary reaches `MainPID=0`, `ExecMainStatus=78`, `NRestarts=0`, with no continuing six-second log growth.

## Non-goals / scope boundary

- Do not change trade normalization, ledger writes, receipts, notifications, OpenD configuration, or retry policy.
- Do not add quote preflight, parse journald, monkeypatch Futu internals, or change the status schema.
- Do not redesign source coordination or move each account/source into a separate process.
- Do not use a constructor-only child: a successful Futu context cannot be safely transferred to the parent, and reconstructing it would duplicate the blocking operation.

## First-principles judgment and direct evidence

- `OpenDTradePushListener.start()` now detects the auth warning and `_run_listener_source_loop()` writes blocked status and returns 78.
- `_coordinate_listener_sources()` receives 78 and joins its source threads, so application-level coordination terminates.
- Production nevertheless remains `active/running`, while Futu warnings continue. The remaining ownership is interpreter shutdown waiting on SDK-created non-daemon threads.
- A local reproduction shows a `multiprocessing.Process` target that merely returns/SystemExits still remains alive when it created a non-daemon thread. Therefore process isolation alone is insufficient: the isolated child must finish through `os._exit(code)` after flushing standard streams.
- The parent supervisor contains the hard exit to the dedicated trade-intake child. The operator CLI and systemd-facing process retain normal Python lifecycle and receive an ordinary child exit status.

## Affected files/modules

- `src/application/trades/process_supervisor.py` — new narrow process boundary owned by trade intake.
- `src/interfaces/cli/run_ops.py` — delegate the long-running trade-intake command to the supervisor.
- `tests/test_trades_process_supervisor.py` — process lifecycle and exit-code regressions.
- `tests/test_service_deploy.py` — preserve CLI argument delegation contract.

No public command, config, status schema, or systemd unit shape changes.

## Contract / state-machine changes

External contract remains:

```text
./om run trade-intake -> integer exit status
```

Internal lifecycle becomes:

```text
CLI parent
  -> spawn dedicated trade-intake child
  -> child runs existing auto_intake.main(argv)
  -> child flushes stdout/stderr
  -> child os._exit(application_code)
  -> parent joins and returns child exit code
```

Terminal auth state remains absorbing:

```text
SDK warning -> blocked status persisted -> application code 78
-> isolated child hard-exits 78 -> parent returns 78
-> systemd does not restart
```

Signal/cancellation behavior:

- systemd's default control-group kill reaches parent and child;
- parent `KeyboardInterrupt` terminates and joins the child, preserving the prior operator-facing clean-stop behavior;
- a child terminated by signal maps to a conventional non-zero shell status rather than being mistaken for success.

## Implementation decision

Use the standard-library `multiprocessing` spawn context for one dedicated child around the entire existing auto-intake application. The child entrypoint imports `auto_intake.main`, captures its integer result, flushes stdout/stderr, and calls `os._exit(code)` in `finally`-safe outer code. It must never invoke the CLI recursively.

Why this is not over-designed:

- one supervisor and one child are the minimum boundary that can own all Futu threads and discard them safely;
- constructor-only isolation cannot transfer a live SDK context;
- changing every source to a process would expand state, IPC, and write coordination without current need;
- calling `os._exit` in the main CLI process would be shorter but would make all parent cleanup and embedding semantics unsafe.

## Slice 1 — deterministic trade-intake process boundary

Objective: introduce the child boundary and preserve CLI behavior.

Allowed files:

- `src/application/trades/process_supervisor.py`
- `src/interfaces/cli/run_ops.py`
- `tests/test_trades_process_supervisor.py`
- focused existing CLI tests in `tests/test_service_deploy.py`

Exact changes:

- add a top-level, spawn-picklable child entrypoint;
- run existing `auto_intake.main(argv)` only inside that child;
- flush output and hard-exit the child with the application's code;
- return the child exit code from the parent;
- handle operator interruption by terminating/joining the child;
- reject an impossible/missing exit code as failure;
- update CLI delegation to call the supervisor instead of importing application `main` directly.

Tests:

- a real spawned child that starts a non-daemon thread still terminates promptly through the hard-exit helper;
- exit 78 is preserved by the parent;
- ordinary non-zero exit is preserved;
- signal termination is non-zero;
- KeyboardInterrupt cleanup terminates/joins a fake child;
- existing CLI argument forwarding remains unchanged.

Stop condition: any evidence that spawn cannot initialize safely under the packaged/runtime entrypoint, or that the parent cannot preserve status/exit semantics.

## Validation

Focused:

```bash
python3.12 -m pytest tests/test_trades_process_supervisor.py tests/test_service_deploy.py -q
python3.12 -m pytest tests/test_trades_push_listener.py tests/test_trades_auto_intake_restart_policy.py -q
python3.12 -m compileall -q src/application/trades src/interfaces/cli
```

Broader before draft PR:

```bash
python3.12 -m pytest tests/test_trades_*.py tests/test_service_deploy.py tests/test_service_drift.py -q
python3.12 scripts/generate_dependency_graph.py --check
python3.12 -m pytest -q
python3.12 tests/run_smoke.py
```

Expected assertions: no regression in current auth classification/status behavior; process supervisor exits promptly and preserves 78; public CLI arguments and generated service command remain unchanged.

## Docs decision

No user documentation change: command/config/output contracts are unchanged. Durable Gateflow and review artifacts record the lifecycle change.

## Risks / open questions

- `spawn` adds one Python startup and config load only once at service start; acceptable for a long-running listener.
- `os._exit` skips child interpreter finalizers; this is intentional only after the application has returned and persisted its terminal state. stdout/stderr are explicitly flushed first.
- abrupt SIGKILL cannot persist a final status; unchanged operational risk, handled by systemd state.
- production rollout remains blocked until code review, full validation, merge/release approval, explicit upgrade, and a single canary.

## Completion report

Report branch/commits, artifacts, tests, PR/check status, release/remote state if separately approved, process exit status, systemd restart count, blocked artifact, and journal stabilization evidence.
