# Close target error layering

Context:

After explicit close target resolution was added, an auto-close identity mismatch failed at the resolver layer with `target_identity_mismatch` before reaching the older preflight `target_contract_mismatch` guard.

Lesson:

- Tests for close target mismatch should assert the active fail-closed layer, not assume the deepest preflight guard always produces the error.
- Earlier failure is acceptable when it carries the same or stronger identity evidence and blocks the write before event construction.

Validation:

- `tests/test_ledger_maintenance.py::test_auto_close_expired_positions_fail_closed_on_ledger_identity_mismatch`
