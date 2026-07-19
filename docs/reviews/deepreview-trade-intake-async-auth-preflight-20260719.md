# Aggregate Deep Review — trade-intake-async-auth-preflight

- Gate: aggregate deepreview / re-review
- Base: `origin/main` v1.2.416
- Decision: pass

## Production failure closure

The patch moves observation ahead of the blocking constructor completion by running the exact trade-context constructor on a daemon worker and observing its scoped SDK warning stream. It does not repeat the failed post-construction health-check assumption or the blocked quote-context experiment.

## Findings

No accepted findings.

## Correctness and recovery

- Only `init connect fail` records naming `OpenSecTradeContext` are eligible for terminal classification.
- Existing classifier remains the sole phone-verification policy owner.
- Auth and coordinated cancellation abandon a daemon worker only while the process is exiting; no timeout-driven retry can accumulate workers.
- Successful construction transfers the real context and handler unchanged.
- Cancellation is handled before generic retry in the source loop.
- v1.2.416 exit 78, blocked status, multi-source propagation, and generated systemd policy remain intact.

## Validation

- focused and broader trade-intake/watchdog/service-deploy suite: 163 passed;
- compileall: passed;
- diff check: passed.

## Residual risks

- Runtime behavior depends on the pinned Futu SDK continuing to emit the initialization warning to `FTConsoleLog`; assigned to immediate production canary.
- Production unit content still requires explicit render/install; assigned to rollout.
