# Gateflow S4 Scope Amendment — data-storage-runtime-projection-p3a

- Gate: `plan amendment before S4 implementation`
- Work unit: `data-storage-runtime-projection-p3a`
- Recorded at: `2026-08-14 07:45:09 +0800`
- User decision: `confirmed`
- Artifact path:
  `docs/gateflow/data-storage-runtime-projection-p3a/s4-scope-amendment.md`

## Blocking fact

S4 requires every event-writing facade to use one transactional projection
runtime without changing public result/error behavior. The S3 runtime owns the
correct fast/full selection and already has a private in-transaction core, but
its public entrypoints always open and commit their own transaction. Facades
that also persist Combo identity, lifecycle allocations, notification outbox,
or other adjunct rows therefore cannot use those entrypoints without breaking
atomicity.

The existing facade results also expose full-projection diagnostics. The S3
runtime does not return them. Code review subsequently confirmed every current
diagnostic is severity `error`, so successful results always carry the exact
empty list and error-bearing projections fail before publication. S4 must
preserve that contract without a second O(E) replay.

## Confirmed minimal amendment

S4 may additionally modify
`src/application/ledger/position_projection_runtime.py`, only to:

1. expose an application-internal entrypoint for an already-open SQLite
   transaction by reusing the existing S3 runtime core;
2. preserve input-order event-created flags needed by existing facade results;
3. return the exact empty diagnostic list on success and preserve the full
   oracle's fail-before-publication behavior for every current diagnostic.

The caller remains transaction owner. The runtime entrypoint must neither
commit nor roll back it. No new schema, cache, authority, mode, configuration,
or production activation is allowed.

## Required validation

- caller-owned rollback covers event, lot, head, and checkpoint writes;
- existing external runtime entrypoints retain their commit/rollback behavior;
- created flags preserve candidate order and idempotent retry behavior;
- fast/unchanged paths never load the full event prefix or full lot list;
- an error-bearing full projection remains fail-closed.

## Goal alignment

This does not expand the confirmed goal. It is the minimum missing adapter
required to preserve the already-approved atomic writer boundary, diagnostic
contract, and O(tail) fast-path success signal.

## Status

`accepted after targeted plan review`

- Review: `docs/reviews/plan-review-20260814-074509.md`
- Conclusion: `pass`
