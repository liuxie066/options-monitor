## Context

No-send tick runs still prepare account messages and need to show what would
have been sent, but downstream state consumers should not treat them as real
notification delivery.

## Pattern

- Keep `sent` false when `no_send` is true.
- Preserve would-send details separately, using `would_send_accounts`.
- Keep `sent_accounts` for confirmed delivery only.
- Add regression tests at the shared state boundary, not only at run metrics.
