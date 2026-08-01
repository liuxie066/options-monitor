# Gateflow S3 Implementation

- Gate: implementation S3
- Work unit: `lifecycle-receipt-batching`
- Accepted S2 commit: `032a9dde`
- Status: implementation complete; pending code review

## Implemented scope

- Added one process-owned `LifecycleReceiptBatchDispatcher` with a cancellable one-second poll, one durable batch attempt per poll and orderly close before other listener resources are closed.
- Resolved one account allow-set from enabled sources using source receipt precedence. Dry-run, fully disabled receipt scope and unavailable routes do not start a dispatcher.
- Removed the five-second account-scoped delivery block from `_run_listener_source_loop()`. Source loops continue to write lifecycle facts and intents but no longer own provider delivery.
- Kept planner/claim/send-started/completion in their existing short SQLite transactions while provider I/O runs outside both SQLite transactions and the shared `process_lock`.
- Added a sanitized dispatcher snapshot to each source's `lifecycle_delivery` status. It contains provider/channel/fingerprints and summarized durable results, never the raw route target or frozen member payloads.
- Updated the lifecycle operator documentation with process ownership, cancellation, non-blocking write behavior and disabled/unavailable status reasons.

## Storm and concurrency evidence

- A real dispatcher fixture with 15 `lx` plus 9 `sy` intents on one route produced exactly one fake provider call, one 24-member batch and atomic confirmation of all members.
- A blocking fake provider call left an independent ledger write under the normal process lock able to complete before provider release, proving the dispatcher holds neither the process lock nor a SQLite transaction over provider I/O.
- Single- and two-source `main()` fixtures each constructed, started and closed exactly one dispatcher; every source received the same global status callback.
- Static ownership regression proves `_run_listener_source_loop()` contains no lifecycle dispatch or provider-send call.
- Cancellable-wait regression proves close wakes a one-second poll immediately and reaches `stopped`.

## Validation

- Focused S3 runtime suite: `68 passed`.
- Full work-unit focused suite through S3: `168 passed`.
- Ruff on changed S3 production and test files: pass.
- Python compile checks: pass.
- `git diff --check`: pass.

## Safety boundaries

- All delivery tests use fake senders and temporary SQLite ledgers; no real notification was sent.
- No production config, runtime ledger, service, Release, deployment or remote environment was changed.
- Cross-process ownership remains a rollout prerequisite, as classified in the accepted plan; S3 establishes one owner inside one listener process.
