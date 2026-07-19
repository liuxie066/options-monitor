# Aggregate Deep Review — trade-intake-auth-aware-restart

- Gate: aggregate deepreview / fix / re-review
- Base: `origin/main` (`0bb5b690`)
- Reviewed target: all branch changes through slice 3
- Decision: pass after fixes

## Findings

### DR-1 — Medium — accepted plan artifacts still claimed one-second polling

Implementation review correctly reduced steady-state `get_global_state()` polling to five seconds to avoid creating a new request/rate-limit source, but the accepted plan and plan re-review still stated one second. This would make the final evidence internally contradictory and could cause a later maintainer to “restore” the rejected cadence. Fixed both artifacts to state immediate startup observation followed by five-second polling.

### DR-2 — Medium — operator recovery behavior was not in public troubleshooting documentation

Exit 78 plus `RestartPreventExitStatus=78` intentionally leaves trade intake stopped until authentication is completed and an operator starts it. Without a public recovery hint, the safe fail-stop behavior can look like an outage. Fixed the README troubleshooting table with the status/error evidence and manual recovery boundary.

## Correctness review

- Existing trade context is observed; no quote context or readiness proxy is introduced.
- Only classifier code `OPEND_NEEDS_PHONE_VERIFY` maps to terminal exit 78.
- Generic context failures remain retryable with a floor and 60-second cap.
- Multi-source terminal auth sets the shared event before result propagation and is returned by the coordinator.
- systemd prevents restart only for exit 78 on trade intake; unrelated services are unchanged.

## Stability / maintainability review

- One dedicated exception and one stable exit code are sufficient; no generic supervisor framework was added.
- Status artifacts preserve classifier code, message, detail, stage, and restart count.
- Existing trade write, receipt, backfill, and mapping paths are unchanged.

## Validation

- focused trade intake, watchdog/error-policy, and service deployment suite: 159 passed.
- compileall for changed application modules: passed.
- `git diff --check`: passed.

## Residual risks

- Futu may emit phone-verification only asynchronously and not return it from the trade context global-state call: assigned to production canary after release.
- Five-second steady-state state checks add bounded OpenD traffic: assigned to production observation.
- Production unit rendering/installation and service start/stop require a separate CEO-authorized rollout.
