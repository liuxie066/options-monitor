# Plan Review — trade-intake-auth-aware-restart

- Gate: plan review
- Target: `docs/gateflow/trade-intake-auth-aware-restart-plan.md`
- Review mode: adversarial implementation-plan review
- Decision: changes required

## Findings

### PR-1 — High — Health polling cadence is underspecified and the existing loop can wait 60 seconds

The plan says to poll during the heartbeat/backfill loop, but the current loop ends with `stop.wait(60)`. Production emits many SDK reconnect attempts in 60 seconds. The implementation needs an explicit short health interval independent of the 60-second status heartbeat/backfill cadence. Use a small fixed poll interval (for example one second) without changing backfill scheduling.

### PR-2 — High — Multi-source result collection can deadlock

If one source returns 78 and the main thread is blocked joining another source, the terminal result may not be observed in time. The plan must require a result queue/coordinator loop that observes completed source results while threads are alive, sets the shared stop event immediately on 78, then joins all threads.

### PR-3 — Medium — Successful `start()` is not evidence that retry delay should reset

The SDK can return from `start()` while authentication is still failing asynchronously. Resetting backoff immediately after `start()` can keep repeated transport failures at the minimum delay. Reset only after at least one healthy observation from the existing trade context.

### PR-4 — Medium — Error classifier inputs need an explicit contract

Futu may return either a state dict or `(ret != 0, error text)`. The listener method must pass state and error separately to the existing classifier, preserve the classifier code/message on the terminal exception, and avoid depending on an imported Futu `RET_OK` constant in tests if `ret == 0` is the existing SDK contract.

## Architecture boundary review

Pass with the above corrections. Application listener code may depend on infrastructure classification, while systemd policy remains in service deployment. No domain dependency reversal is introduced.

## Best-practice / optimal-solution review

Using the existing trade context is superior to a quote probe or a second connection. Native systemd `RestartPreventExitStatus` is the minimal lifecycle mechanism. A stable sysexits-style code avoids a new config key.

## Overengineering / coupling review

No need for a generic retry framework, new daemon supervisor, notification path, or authentication state machine. Keep one exception, one exit code, one optional renderer field, and a small coordinator.

## Residual risks

The asynchronous-only SDK failure risk remains assigned to production canary. It does not justify log scraping inside the process.
