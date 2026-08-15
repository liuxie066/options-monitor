# Gateflow Plan Fix — Sell Put Top1 W3

- Gate: `plan review -> fix`
- Work unit: `sell-put-top1-w3`
- Plan: `docs/gateflow/sell-put-top1-w3/plan.md`
- Review: `docs/reviews/plan-review-20260815-113103.md`
- Artifact path: `docs/gateflow/sell-put-top1-w3/plan-fix.md`

## Finding decisions

### PR-W3-01 — accepted — fixed

Hidden commitments now use an experiment-local content-addressed path. A file published before a failed start transaction has no SQLite authority, and a changed proposal uses a different path. The acceptance matrix covers publish-before-commit failure followed by a changed valid proposal.

### PR-W3-02 — accepted — fixed

Enable now requires exact maintainer availability and performs no SQLite write while unavailable. Disable remains unconditional and idempotently commits account opt-out before reconciling active experiments.

### PR-W3-03 — accepted — fixed

The existing commitment table now stores 20 lightweight date-occupancy rows with a unique `(market, account, strategy_family, trading_date)` constraint. The canonical payload/hash/ref remains stored once; no seventh table was added. Exact-date overlap replaces interval overlap.

### PR-W3-04 — accepted — fixed

W3 day 20 now always closes decision intake into `awaiting_outcomes`, requests the hidden generation terminal, and releases the slot. W6 owns the job-aware transition to `ready_to_conclude`, including the empty outcome seal. W3 no longer presents absence of a job table as evidence of no jobs.

### PR-W3-05 — accepted — fixed

The public `complete_experiment()` command was removed from W3. W3 concludes only deterministic aborted receipts and retains the generation terminal CAS/recovery primitive. W5/W6 add successful completion only with their exact result schema and authority.

### PR-W3-06 — accepted — fixed

Each mutation now has an explicit semantic subject key. Natural facts are unique on `(event_type, subject_key)`, while a caller idempotency key may only replay the same canonical payload inside its command scope. Tests cover different keys targeting one fact and one key targeting different bytes.

### PR-W3-07 — accepted — fixed

The spec rule now freezes only the research hash domain after research starts. After the research terminal is published, validation-only fields may be added or changed before validation starts while the exact research hash remains immutable; each change invalidates validation authorization.

## Decision

`fix complete`; next gate: `plan re-review`.
