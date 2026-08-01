# Gateflow Aggregate Validation

- Gate: aggregate validation
- Work unit: `lifecycle-receipt-batching`
- Base: `origin/main@51275d59`
- S3 accepted commit: `8ca39a73`
- Status: pass

## Deterministic acceptance evidence

- Historical storm fixture: `lx=15`, `sy=9`, one route, one real dispatcher object, exactly one fake sender call.
- Durable result: one 24-member batch and all 24 member intents atomically `confirmed`.
- Rendering: at most 12 representatives displayed while all 24 members remain frozen and settled.
- Rate limit: a new intent remains idle until 60 seconds after the previous route `send_started`.
- Retry: explicit failures reuse one batch/transport key and stop after attempt three.
- Ambiguity: exception, accepted and stale send-started do not auto-resend.
- Migration: legacy suppressed/confirmed rows remain terminal and unbound.
- Ownership: source listener has no lifecycle provider call and the legacy per-row dispatcher is absent.
- Concurrency: an independent ledger write completes while a fake provider call is blocked.

## Test and static gates

- Work-unit focused suite: `169 passed`.
- Related lifecycle/maintenance/multi-tick suite: `122 passed`, four pre-existing renderer deprecation warnings.
- Full suite: initial final-code sandbox run reported `3951 passed`, `10 skipped`, one loopback-bind permission failure and one stale generated-graph failure.
  - Regenerated the dependency graph after the final aggregate hardening; generator check and its two tests passed.
  - Re-ran the read-only HTTP test outside the restricted socket sandbox; `1 passed`.
  - Effective final result: all `3953` non-skipped full-suite tests passed; `10 skipped`.
- Ruff across every changed Python production/test file: pass.
- Python compile checks for the new runtime owner and modified listener: pass.
- `git diff --check`: pass.
- Dependency graph: `907` Python files, `577` production modules, `0` parse errors, `0` production cycles; boundary guard pass.

## Safety evidence

- Provider tests use fake senders only.
- All ledgers are temporary test SQLite stores.
- No production configuration, runtime data, service state, real notification, Release, deployment or remote environment was changed.
