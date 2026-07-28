# Holdings Domain DeepReview Remediation

## Scope

- Source review: `docs/reviews/repo-review-20260729-010955.md`
- Findings closed: 32 / 32
- Production writes, notifications, broker calls, releases, and upgrades: not performed
- Runtime configuration files: not modified

## Closeout Matrix

| # | Status | Owning boundary | Primary verification |
|---:|---|---|---|
| 1 | Fixed | Transactional ledger writer now rejects any projection with error diagnostics before commit. | `tests/test_ledger_sqlite_workflows.py` |
| 2 | Fixed | Strategy adjustments are part of the domain projection; publisher consumes only active, accepted adjustments. | `tests/test_ledger_projection.py`, `tests/test_ledger_publisher.py` |
| 3 | Fixed | Rebuild/bootstrap/CLI fail closed and preserve the last valid projection when replay has errors. | `tests/test_ledger_sqlite_workflows.py`, `tests/test_option_positions_cli.py` |
| 4 | Fixed | Projection checkpoint includes an explicit projection contract version. | `tests/test_ledger_sqlite_workflows.py` |
| 5 | Fixed | Canonical events reject invalid time, source, currency, strike, multiplier, non-finite/negative price, and non-finite fees before storage. A finite zero price remains valid because the domain supports zero-cost opens and zero-price expiries. | `tests/test_ledger_event_codec.py`, `tests/test_performance_capital.py` |
| 6 | Fixed | Projection validates target existence, uniqueness, self-reference, and forbidden control-event targets. | `tests/test_ledger_projection.py` |
| 7 | Fixed | Lifecycle contract and stock-evidence identity includes broker. | `tests/test_ledger_maintenance.py` |
| 8 | Fixed | Automatic expiry commits terminal event, projection, allocation, and lifecycle case atomically. | `tests/test_ledger_maintenance.py` |
| 9 | Fixed | Manual open/assignment/exercise use stable request identity and intent hashes across preview/apply/retry; inbound and option-intake propagate that identity. | `tests/test_option_positions_cli.py`, `tests/test_inbound_control.py`, `tests/test_option_intake_command.py` |
| 10 | Fixed | Ledger read failures propagate as unavailable/errors; only a successful empty query means zero holdings. | `tests/test_positions_context_builder_partial_close.py`, `tests/test_option_positions_cli.py` |
| 11 | Fixed | Successful manual writes invalidate derived position-context caches. | `tests/test_positions_context_builder_partial_close.py` |
| 12 | Fixed | Public history is event-first and can audit a voided/tombstoned lot. | `tests/test_option_positions_cli.py` |
| 13 | Fixed | Broker deal identity is account-scoped across intake, backfill, state, and lifecycle evidence. | `tests/test_trades_auto_intake_backfill.py`, `tests/test_trades_resolver_open.py` |
| 14 | Fixed | Partial stock settlements use the v2 lifecycle allocation path and fail closed on inconsistent quantity. | `tests/test_trades_resolver_close.py` |
| 15 | Fixed | Auto-intake accepts explicit manual deal JSON source paths without relying on environment-only discovery. | `tests/test_trades_auto_intake_audit.py` |
| 16 | Fixed | Performance consumes the active adjusted event state and excludes voided adjustments. | `tests/test_performance_engine.py` |
| 17 | Fixed | Cash conversion has one canonical validation contract for observed conversion evidence. | `tests/test_cash_conversion_at_write.py`, `tests/test_cash_conversion_backfill.py` |
| 18 | Fixed | Assigned-stock projection applies active-event/void semantics and account/broker scope consistently. | `tests/test_performance_assignment.py` |
| 19 | Fixed | Projection verification is read-only by default; publishing evidence requires an explicit flag. | `tests/test_option_positions_cli.py` |
| 20 | Fixed | Close Advice fails closed when required position context is unavailable. | `tests/test_close_advice_runner.py` |
| 21 | Fixed | Close Advice cache receipts bind quote source, observation time, run, and freshness. | `tests/test_close_advice_quote_cache.py`, `tests/test_close_advice_runner.py` |
| 22 | Fixed | Close Advice report manifests bind market/account/run; fallback rejects cross-market reports. | `tests/test_close_advice_runner.py` |
| 23 | Fixed | One-shot Close Advice uses request-scoped context/report roots and guarded publication. | `tests/test_agent_plugin_smoke.py`, `tests/test_close_advice_runner.py` |
| 24 | Fixed | Position Advice current pointers are market-scoped and publication is generation-monotonic. | `tests/test_position_advice_runner.py`, `tests/test_position_advice_v2_input_builder.py` |
| 25 | Fixed | Notification authority has explicit attempt, failed-retry, delivered-dedupe, unknown, and stale-inflight recovery states. | `tests/test_position_advice_notification_authority.py` |
| 26 | Fixed | First-use authority bootstrap uses resumable staged generations instead of unrecoverable partial history. | `tests/test_position_advice_v2_authority_service.py` |
| 27 | Fixed | Promotion readiness reuses the actual authority preflight, including outstanding notification state. | `tests/test_position_advice_promotion.py` |
| 28 | Fixed | Model trade actionability and mandatory human review are separate; lifecycle review remains visible as P0. | `tests/test_position_advice_plan_builder.py`, `tests/test_daily_decision_brief_service.py` |
| 29 | Fixed | PM valuation responses are schema-, account-, snapshot-, freshness-, and trust-validated; missing/stale/untrusted evidence cannot be complete. | `tests/test_portfolio_management_client.py`, `tests/test_portfolio_assignment_scenario.py` |
| 30 | Fixed | PM holdings-sync receipts prove account, non-dry-run source, durable receipt, and all stages; uncertain transport results require explicit reconciliation. | `tests/test_portfolio_holdings_sync_client.py`, `tests/test_trades_stock_holdings_sync.py` |
| 31 | Fixed | Feishu portfolio freshness uses owner observation/update time, never retrieval time; missing timestamps are unknown. | `tests/test_fetch_portfolio_context_richtext.py`, `tests/test_position_advice_source_producers.py` |
| 32 | Fixed | Futu settings merge per key and require explicit `REAL` or `SIMULATE`; invalid/missing environments fail closed before gateway construction. | `tests/test_futu_portfolio_context.py`, `tests/test_validate_config_notifications.py` |

## Final Verification

- Full test suite: `3558 passed, 10 skipped`
- Ruff: passed
- Python compileall: passed
- Dependency graph: current, `production_modules=549`, `cycles=0`
- `git diff --check`: passed
- US/HK example config validation: passed
- US/HK example config build dry-runs: passed with `write_applied=false`

## Deployment Precondition

The current runtime `config.us.json`, `config.hk.json`, and `config.yaml` do not
declare Futu `trd_env`. The code now fails closed instead of silently selecting
`REAL`. A separately authorized runtime-config migration must explicitly choose
`REAL` or `SIMULATE` before any deployment that needs Futu portfolio context.
