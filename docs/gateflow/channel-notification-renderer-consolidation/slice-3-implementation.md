# Gateflow Slice 3 Implementation — Shared System Notice shell

- Gate: `implementation`
- Work unit: `channel-notification-renderer-consolidation`
- Slice: `3`
- Baseline: `146e94cd gateflow: accept channel-notification-renderer-consolidation slice-2`
- Status: `implementation complete; pending code review`
- Artifact path: `docs/gateflow/channel-notification-renderer-consolidation/slice-3-implementation.md`

## Scope and outcome

Implemented the approved presentation-only System Notice consolidation:

- Added `render_system_notice()` in `src/application/notification_shells.py`.
- OpenD failure and recovery now submit caller-owned status/fields to the shared shell.
- Delivery-failure summaries now submit caller-owned batch fields and per-account diagnostic rows to the same shell.
- All titles use `# OM · 系统通知 · <component>`.
- Multiline fields and section rows are flattened to one visible line.
- Empty sections are omitted.
- OpenD still owns the 1200-character raw detail limit, error code, cooldown/burst/consecutive thresholds, recovery gate, and send decision.
- Scheduled notification still owns account aggregation, retry, message/provider diagnostics, and failure-summary delivery.

## Changed files

- `src/application/notification_shells.py`
- `src/application/multi_tick/opend_guard.py`
- `src/application/scheduled_notification.py`
- `tests/test_notification_shells.py`
- `tests/test_opend_watchdog_alerts.py`
- `tests/test_scheduled_notification_application.py`

## Minimal design

The shell only owns:

1. one standalone H1;
2. `状态｜...` plus ordered caller fields;
3. omission of empty sections;
4. newline flattening for labels, values, section titles, and rows.

It does not send, retry, persist, truncate business details, classify OpenD failures, or inspect provider configuration.

## Validation

```text
PYTHONPYCACHEPREFIX=/tmp/om-pycache python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_notification_shells.py \
  tests/test_opend_watchdog_alerts.py \
  tests/test_scheduled_notification_application.py \
  tests/test_feishu_bot.py

42 passed
```

The matrix covers:

- OpenD failure and recovery;
- no-send/rate-limit/recovery behavior;
- all-account and partial delivery failure;
- multiple failed accounts;
- multiline error/provider diagnostics;
- provider response code, message ID, attempt count and confirmation status;
- P0 Feishu single-md-node identity for a real failure-summary renderer.

Static validation:

- Ruff changed files: pass
- compileall changed source: pass
- `git diff --check`: pass

## Docs decision

No separate docs update is required in this slice: the public family names and transport identity are already documented in the accepted plan/AGENT_WIKI, while this slice only consolidates internal presentation ownership.

## Residual risks

- Receipt shell integration is covered by approved Slice 4.
- Live Feishu/WeChat client rendering remains outside current authorization.
- Failure-summary delivery failure semantics remain unchanged and are covered by aggregate validation.
