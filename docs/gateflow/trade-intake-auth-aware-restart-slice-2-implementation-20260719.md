# Implementation — trade-intake-auth-aware-restart slice 2

- Gate: implementation
- Scope: process exit, bounded retry, multi-source propagation
- Changed: `src/application/trades/auto_intake.py`, `tests/test_trades_auto_intake_restart_policy.py`
- Decisions: exit 78 on terminal auth; blocked status evidence; five-second health polling; capped exponential retry; queue-based sibling cancellation.
- Validation: focused listener/auto-intake suites -> 19 passed.
- Docs: public CLI unchanged.
- Residual risks: actual SDK state exposure requires production canary; unexpected worker crashes return nonzero but remain systemd-retryable.
- Status: complete pending review.
