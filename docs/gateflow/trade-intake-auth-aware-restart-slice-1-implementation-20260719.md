# Implementation — trade-intake-auth-aware-restart slice 1

- Gate: implementation
- Scope: listener-owned trade-auth observation
- Changed: `src/application/trades/push_listener.py`, `tests/test_trades_push_listener.py`
- Decision: added health observation on the existing trade context and a terminal exception carrying classifier evidence.
- Validation: `python3 -m pytest -q tests/test_trades_push_listener.py` -> 4 passed.
- Docs: no public command changed.
- Residual risks: actual SDK response remains production-canary-owned; non-phone auth modes remain retryable by design.
- Status: complete pending review.
