# Implementation — trade-intake-process-isolated-auth-preflight / slice 1

- Gate: implementation
- Status: complete

## Changes

- Added `src/application/trades/process_supervisor.py`.
- The CLI parent starts one spawn-context child for the complete auto-intake application.
- The child calls `auto_intake.main(argv)`, flushes stdout/stderr, then uses `os._exit(code)` so SDK-owned non-daemon threads cannot hold interpreter shutdown open.
- The parent propagates normal exit codes, maps signal exits to conventional shell codes, and terminates/joins the child on operator interruption.
- `run_ops` now delegates trade-intake to the supervisor while preserving its injectable callable and argument contract.
- Added a real subprocess regression proving exit 78 is prompt even with a live non-daemon thread.

## Validation

```text
105 passed — process supervisor + service/CLI delegation
12 passed — push listener + auto-intake restart policy
compileall passed — trades + CLI packages
284 passed — broad trades + service-deploy regression
dependency graph regenerated/current — 469 production modules, 0 cycles
git diff --check passed
```

## Residual risks

- Production Futu behavior still requires a release canary: covered by the separately approved rollout phase after PR/release authorization.
- Child interpreter finalizers are skipped: fixed/contained by the dedicated process boundary and explicit stream flush after application state writes.
- No notification, ledger, trade, config, or service mutation occurred in this implementation slice.
