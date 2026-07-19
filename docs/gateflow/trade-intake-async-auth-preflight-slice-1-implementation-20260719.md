# Implementation — trade-intake-async-auth-preflight slice 1

- Gate: implementation
- Changed: push listener construction monitor, source-loop cancellation, focused tests.
- Decision: monitor the real blocking trade constructor on a daemon worker; classify only scoped SDK init warnings; no quote context or timeout retry.
- Validation: focused listener/restart-policy tests pass.
- Residual risk: current futu-api logger contract is adapter-owned and must be verified in production.
