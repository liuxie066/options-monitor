# Gateflow S2 Implementation — Early identity receipt and failure liveness

- Gate: `implementation S2`
- Work unit: `hk-combo-capture-failure-notification`
- Accepted S1 commit: `7241fe9a`
- Status: implementation complete; pending code review

## Implemented scope

- Added one source-owner helper that locates the deterministic current-run
  portfolio producer directory, publishes the existing receipt contract once, or
  strictly validates and reuses the original payload and receipt bytes.
- Reuse validates source kind, account, run, broker, markets, normalized source,
  portfolio identity, producer policy, observation time, payload path, payload
  content and hashes. Incomplete, ambiguous, stale, symlinked or conflicting
  state fails closed and is never repaired by creating a second identity.
- Routed the full Position Advice source graph through the same helper, so its
  portfolio dependency preserves the first receipt path, snapshot id and
  `completed_at`.
- Added a narrow `account_run` facade that derives broker and markets from the
  frozen account config and maps source-owner failures to the existing typed
  account-config boundary.
- Fresh tick execution now publishes the current-run portfolio receipt after
  prepared portfolio and option authority validation, but before the shared
  required-data provider barrier. A receipt conflict removes only that account
  from prefetch and account execution.
- Recovery validates and reloads the frozen current-run portfolio and option
  manifests, then calls the same owner helper. It never refreshes broker data or
  synthesizes identity from another run.
- Pipeline-nonzero and terminal-barrier outcomes retain the early receipt, so the
  existing Daily Brief authority can authorize only its bounded
  `fixed_failure` path. Typed config/identity conflicts remain `should_notify`
  false.

## Tests added or strengthened

- Source-owner tests prove current identity readability, exact-byte full-graph
  reuse, a single deterministic content directory, and fail-closed behavior for
  tampered, stale, wrong-run, wrong-account, wrong-market, context-drift and
  incomplete-directory inputs.
- Tick barrier tests read the real current-run identity inside the fake provider,
  proving publication occurs before prefetch. They also cover terminal barrier
  failure, account-scoped conflict before provider/account execution, and
  recovery reuse without a second provider call or receipt rewrite.
- The account runner pipeline-nonzero regression proves the receipt survives the
  early return and the result remains notification-eligible.
- Daily Brief service coverage now uses the owner helper instead of manually
  invoking the low-level producer.
- The integrated no-send regression runs the real Daily Brief assembler against
  the owner receipt, observes `authority_identity_source=current_run_portfolio_receipt`,
  selects `decision=fixed_failure`, records no provider call and persists no
  delivery envelope.
- Notification-flow authority fixtures now derive their test identity through
  the canonical portfolio identity function instead of an unrelated test-only
  hash shape.

## Validation

- Focused S2 suite covering all eight allowed S2 test modules plus
  `tests/test_multi_account_tick.py`: `180 passed`.
- The two direct owner-to-Daily-Brief chain regressions: `2 passed`.
- Ruff over every changed S2 Python file: pass.
- `python3.12 -m py_compile` over every changed S2 Python file: pass.
- `git diff --check`: pass.

After the initial S2 review, the suite count increased from 178 to 180 with
invalid-UTF-8 and wrong-broker receipt conflicts.

The repository `.venv` does not contain `pytest`; validation used
`python3.12 -m pytest -p no:cacheprovider` with an isolated bytecode cache.

## Docs and safety decision

- No public schema, command, configuration, scheduler payload, notification
  wording or authority policy changed. Gateflow and review artifacts are the only
  docs in scope.
- No broker refresh, live provider send, runtime write, service mutation,
  release, deployment or remote operation was invoked.
- Pre-existing unrelated dirty files remain untouched and unstaged, including
  `docs/DEPENDENCY_GRAPH.md`.

## Residual risks

- The portfolio observation still uses the existing 30-minute freshness window;
  unusually long runs fail closed rather than silently refreshing identity.
- Scheduler processed-target retry and OpenD expiration timeout/retry remain
  explicitly deferred to separate work units.
- Production remains unchanged until separately authorized release and upgrade
  stages.

## Next gate

`code review S2` using DeepReview.
