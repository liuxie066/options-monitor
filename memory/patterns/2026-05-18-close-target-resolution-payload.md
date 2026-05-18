# Close target resolution payload

Pattern:

- Resolve close targets once in the ledger layer.
- Pass the same resolution payload through preview, diagnostics, operations, and stored event raw payload.
- Use `record_id` / lot id as the write target; keep `position_key` and aggregate views read-only.
- Prefer fail closed over fallback matching when identity fields drift or multiple lots match.

Why It Works:

This makes manual close, broker close, and auto-close auditable against the same target contract, and prevents same expiry, same strike, multi-lot, and cross-expiry misrouting from reappearing in separate workflow code.
