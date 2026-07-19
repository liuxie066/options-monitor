# Plan Review — trade-intake-process-isolated-auth-preflight

- Gate: plan review
- Decision: changes required

## Findings

### PR-1 — High — `multiprocessing` return/SystemExit alone does not defeat non-daemon SDK threads

Direct reproduction shows a multiprocessing child remains alive after its target raises `SystemExit(78)` if that target created a non-daemon thread. The plan must require `os._exit(application_code)` inside the isolated child after explicit stdout/stderr flush; otherwise it reproduces the production failure one process deeper.

### PR-2 — High — the child boundary must wrap the entire intake application, not only context construction

A successful live Futu context is not safely transferable across processes. Constructor-only preflight would require a second construction in the parent and reintroduce the same auth race. The entire existing `auto_intake.main(argv)` must execute in the child; the parent owns only lifecycle and exit propagation.

### PR-3 — Medium — avoid recursive CLI spawn

The child entrypoint must import and call `src.application.trades.auto_intake.main` directly. Calling `./om run trade-intake` or `run_ops` from the child would recursively create supervisors.

### PR-4 — Medium — signal exits must not collapse to success

`multiprocessing.Process.exitcode` is negative for signal termination. The supervisor must map it to a conventional non-zero shell status and tests must cover it. `None` after join is an invariant failure, not zero.

### PR-5 — Medium — parent interruption needs bounded child cleanup

The prior foreground behavior handled `KeyboardInterrupt`. The supervisor must terminate and join the child when interrupted, and the test must assert both actions so local/operator stops do not orphan a child.

## Lens results

- Architecture boundary: pass after fixes; process ownership remains in the trade-intake adapter/CLI boundary and does not leak into domain or ledger layers.
- State machine: pass after fixes; blocked status is persisted before terminal exit, and 78 remains absorbing at systemd.
- Recovery/cancellation: pass after explicit signal mapping and parent interruption cleanup.
- Coupling/simplicity: pass; one child around the complete application is narrower than per-source IPC or constructor transfer.
- Validation: pass after adding a real non-daemon-thread child regression, not only mocked process tests.

## Residual risks

- Spawn startup overhead: accepted, negligible for a long-running service.
- Child finalizers skipped after application return: accepted by design and contained to the intake child.
