# Gateflow Fix Artifact — Option Performance Refactor S9

- **Gate**: code review fix
- **Work unit**: `option-performance-refactor`
- **Slice**: S9
- **Created at**: 2026-07-18 00:38:19 UTC
- **Review source**: `docs/reviews/code-review-20260718-003625.md`
- **Artifact path**: `docs/gateflow/option-performance-refactor-S9-fix-20260718-003819.md`
- **Decision**: accept S9-CR-01 and S9-CR-02

## Accepted Findings and Fixes

### S9-CR-01 — Invalid PM period cash change was treated as absent

- **Decision**: accepted.
- **Fix**: cash facts now distinguish an absent `period_cash_change` from an explicitly supplied but unparsable value. Absence still derives safely from opening/external/ending cash; invalid supplied data returns `portfolio_cash_period_change_invalid` with no bridge amounts.
- **Validation**: added a regression with `period_cash_change="unknown"` proving the account and combined bridge are unavailable.
- **Status**: 已修复.

### S9-CR-02 — Report-level evidence IDs were dropped

- **Decision**: accepted.
- **Fix**: both bridge evidence envelopes now preserve report-level `quality.evidence_fact_ids`, merge any compatibility nested metric IDs deterministically, retain metric `fx_fact_ids`, and expose the complete `report_quality` snapshot.
- **Validation**: PnL and cash tests now inject v1 report-level evidence IDs and assert they survive in `option_pnl_evidence` and `option_cash_evidence`.
- **Status**: 已修复.

### Residual test gap — YTD propagation

- **Decision**: fixed in current slice.
- **Fix/validation**: added a YTD PnL bridge regression proving PM period kind/month/end date align with the option report and produce an `ok` bridge.

## Validation

- Approved focused S9 suite: `48 passed`.
- Focused Ruff: pass.
- `git diff --check`: pass.

## Docs Decision

The public docs already describe aligned end dates and evidence completeness; no additional documentation change was needed for these internal validation fixes.

## Residual Risks and Uncovered Areas

| Risk / area | Classification |
|---|---|
| External PM `/analysis/cash-facts` implementation | assigned to external PM owner; OM unavailable and injected-fact behavior covered in S9 |
| Stale S8 Agent smoke-test monkeypatch | covered by later approved S10 whole-suite closure |
| Repository-wide legacy consumer zero-check and rollback/removal entry point | covered by later approved S10 |

No unclassified residual risk remains.

## Completion Status

- **Fix**: pass
- **Blocking open questions**: none
- **Current gate / next entry point**: S9 code re-review
