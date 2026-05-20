# Memory Index

This is a navigation layer over the project-level LLM wiki. It links durable entries by topic and does not replace the entry text.

Use it at the start of architecture, reliability, release, or module-boundary work. If an entry conflicts with code, tests, runtime evidence, `AGENTS.md`, or `docs/ARCHITECTURE.md`, verify the current source before acting.

## Governance

- [Memory workflow](README.md)
- [Memory update log](log.md)
- Templates:
  - [Decision](templates/decision.md)
  - [Pattern](templates/pattern.md)
  - [Failure](templates/failure.md)

## Ledger, Positions, And Trades

Decisions:

- [Option positions SQLite primary](decisions/2026-04-22-option-positions-sqlite-primary.md)
- [Option positions canonical model](decisions/2026-04-27-option-positions-canonical-model.md)
- [Option positions baseline events verifications](decisions/2026-05-09-option-positions-baseline-events-verifications.md)
- [Trade position ledger redesign](decisions/2026-05-17-trade-position-ledger-redesign.md)
- [Application position/trade namespace consolidation](decisions/2026-05-18-application-position-trade-namespace-consolidation.md)
- [Ledger API runtime boundary](decisions/2026-05-18-ledger-api-runtime-boundary.md)
- [Ledger command/query and read view boundary](decisions/2026-05-18-ledger-command-query-read-view-boundary.md)
- [Ledger contract final core closure](decisions/2026-05-18-ledger-contract-final-core-closure.md)
- [Ledger position fields owner](decisions/2026-05-18-ledger-position-fields-owner.md)
- [Ledger public API boundary](decisions/2026-05-18-ledger-public-api-boundary.md)
- [Ledger result contract boundary](decisions/2026-05-18-ledger-result-contract-boundary.md)
- [Ledger semantic core actions](decisions/2026-05-18-ledger-semantic-core-actions.md)
- [Ledger write path field contract boundary](decisions/2026-05-18-ledger-write-path-field-contract-boundary.md)
- [Ledger writer result contract](decisions/2026-05-18-ledger-writer-result-contract.md)
- [Position lot field patch contract](decisions/2026-05-18-position-lot-field-patch-contract.md)
- [Position lot projection patch decoder](decisions/2026-05-18-position-lot-projection-patch-decoder.md)
- [Position lot record write contract](decisions/2026-05-18-position-lot-record-write-contract.md)
- [Position lot sync metadata patch contract](decisions/2026-05-18-position-lot-sync-metadata-patch-contract.md)
- [Position/trade namespace test and doc closure](decisions/2026-05-18-position-trade-namespace-test-doc-closure.md)
- [Retire legacy option position ledger](decisions/2026-05-18-retire-legacy-option-position-ledger.md)
- [Retire webui and position field re-export](decisions/2026-05-18-retire-webui-and-position-field-reexport.md)
- [Sync metadata write API typed only](decisions/2026-05-18-sync-metadata-write-api-typed-only.md)

Patterns:

- [Primary with backup repository](patterns/2026-04-22-primary-with-backup-repository.md)
- [Canonical preflight uses event codec](patterns/2026-05-17-canonical-preflight-uses-event-codec.md)

Failures:

- [SQLite list_records row shape mismatch](failures/2026-04-22-option-positions-sqlite-list-records.md)

## Close Advice And Close Targeting

Decisions:

- [Close advice fee-aware gating and output consistency](decisions/2026-04-28-close-advice-fee-aware-gating.md)
- [Close advice threshold defaults](decisions/2026-05-14-close-advice-threshold-defaults.md)
- [Close target resolution contract](decisions/2026-05-18-close-target-resolution-contract.md)

Patterns:

- [Close events should target lots explicitly](patterns/2026-04-30-close-events-should-target-lots-explicitly.md)
- [Close target resolution payload](patterns/2026-05-18-close-target-resolution-payload.md)

Failures:

- [Close advice threshold fixture](failures/2026-05-14-close-advice-threshold-fixture.md)
- [Close target error layering](failures/2026-05-18-close-target-error-layering.md)

## Tick, Scheduler, Runtime, And Cron

Decisions:

- [Account-scoped multi-tick scheduler](decisions/2026-05-12-account-scoped-multi-tick-scheduler.md)
- [Cron timeout conservative account workers](decisions/2026-05-12-cron-timeout-conservative-account-workers.md)
- [Tick orchestration helper boundaries](decisions/2026-05-13-tick-orchestration-helper-boundaries.md)

Patterns:

- [Batch scheduler state updates](patterns/2026-05-12-batch-scheduler-state-updates.md)
- [Dry-run state should stay observable](patterns/2026-05-12-dry-run-state-should-stay-observable.md)

Failures:

- [Global scheduler state in multi-account](failures/2026-05-12-global-scheduler-state-in-multi-account.md)
- [Overparallelized OpenClaw cron](failures/2026-05-12-overparallelized-openclaw-cron.md)

## Symbols, Candidate Inputs, And Config

Decisions:

- [Market configs are canonical runtime entrypoints](decisions/2026-04-28-market-configs-are-canonical-runtime-entrypoints.md)
- [Shared symbol canonicalization entrypoint](decisions/2026-04-28-shared-symbol-canonicalization-entrypoint.md)

Patterns:

- [Canonicalize symbols at boundaries](patterns/2026-04-28-canonicalize-symbols-at-boundaries.md)
- [Config defaults and domain fallbacks](patterns/2026-05-14-config-defaults-and-domain-fallbacks.md)

## Compatibility, Ownership, And Cleanup

Patterns:

- [Compatibility re-export after owner move](patterns/2026-05-18-compat-reexport-after-owner-move.md)
- [Retire compatibility service after owner migration](patterns/2026-05-17-retire-compatibility-service-after-owner-migration.md)

Failures:

- [Expanded type checking exposes test annotation debt](failures/2026-05-17-expanded-type-checking-exposes-test-annotation-debt.md)
- [Tests can anchor retired compatibility services](failures/2026-05-17-tests-can-anchor-retired-compatibility-services.md)

## Release And Environment

Decisions:

- [Install Futu SDK with bootstrap](decisions/2026-04-27-install-futu-sdk-with-bootstrap.md)
- [Version check via origin tags](decisions/2026-04-27-version-check-via-origin-tags.md)

Failures:

- [Semver dataclass ordering](failures/2026-04-27-semver-dataclass-ordering.md)

## Session Handoffs

Session handoffs live in `memory/sessions/` and are short-term context only. Promote durable lessons into `decisions`, `patterns`, or `failures` before treating them as reusable knowledge.
