## Context

The earlier option positions v2 implementation persisted baseline snapshots,
trade events, and verification snapshots, but projection used the latest
verification snapshot as a new baseline checkpoint. That made the latest
verification authoritative, while hiding the original t0 position fact and
dropping earlier accepted verifications from the projection input.

## Decision

Option positions v2 projection is now:

`BaselineSnapshot(t0 position fact) + PostT0Events + AcceptedVerificationSnapshots -> Projection`

Rules:

1. The persisted baseline snapshot remains the immutable t0 fact for the
   projection lineage.
2. Trade events after baseline time are replayed in timestamp order.
3. Accepted verification snapshots are replayed in the same timeline as
   correction facts, not converted into replacement baselines.
4. A verification snapshot is authoritative for its broker/account scope.
   When no explicit scope is provided, the scope is inferred from the lots in
   that snapshot.
5. Feishu `option_positions` remains a mirror/bootstrap integration. Its field
   schema does not define the projection model.

## Operational Notes

- Existing verification snapshots default to `acceptance_status=accepted`.
- Future staged snapshots can use `acceptance_status=pending` or `rejected`
  and stay out of projection until accepted.
- Intake does not need a separate write path change: broker/manual trades still
  enter as trade events, and the v2 adapter treats those as post-baseline events.
- Feishu sync payload fields do not need to change for this redesign; sync still
  mirrors the compatibility position rows derived from projection.
