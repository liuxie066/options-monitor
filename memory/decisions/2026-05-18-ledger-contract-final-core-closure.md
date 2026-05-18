# Ledger Contract Final Core Closure

## Decision

Core position/trade runtime paths must keep typed result contracts internally and convert to dictionaries only at report, receipt, CLI, JSON, or storage adapter boundaries.

## Applied Contracts

- Auto-close expired: `ExpiredCloseDecision`, `ExpiredCloseApplyResult`, `ExpiredCloseRunResult`.
- Broker trade intake: `BrokerTradeOpenPreviewResult`, `BrokerTradeOperation`.
- Manual void/repair: `TradeEventInterventionPreview`, `TradeEventInterventionLedgerResult`, `LedgerWriteResult`.

## Guardrail

`tests/test_option_positions_legacy_retirement.py` includes structural checks so these paths do not regress to free `dict` operation/result contracts.
