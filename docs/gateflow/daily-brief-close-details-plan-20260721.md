# Gateflow Plan: Daily Brief Close-Position Details

## Gate

- Work unit: `daily-brief-close-details`
- Gate: plan
- Date: 2026-07-21
- Base: `origin/main` at `502c0332`
- Branch: `codex-daily-brief-close-details`
- Goal confirmation: accepted by user

## Goal and Motivation

When the daily decision brief recommends closing a priced position, the compact holding line must answer the operator's three immediate decision questions using existing close-advice facts:

1. the reference close price;
2. the estimated profit locked by closing now;
3. the remaining annualized return from continuing to hold.

The close-advice pipeline already calculates these values. The defect is a projection gap: `_position_view()` discards them and `_position_views()` can therefore render only the action label.

## Success Signal

For an evaluable HK short option with an actionable close recommendation, Markdown includes a nested detail line equivalent to:

```text
- 3690.HK · Sell Put · 08-28 HK$65 Put：建议平仓
  - 参考平仓价 HK$0.50（mid） · 预计锁定收益 HK$1,234.50 · 剩余年化 4.2%
```

The same rendering uses `$` for US positions. Missing or non-finite metrics never produce `None`, `nan`, or invented values.

## Source-of-Truth and Ownership

- Calculation authority remains `domain/domain/close_advice.py` and `src/application/close_advice_runner.py`.
- Structured daily-brief projection is owned by `src/application/daily_decision_brief_service.py`.
- Human Markdown projection is owned by `src/application/daily_decision_brief_renderer.py`.
- No calculation is duplicated in the renderer.

## Scope

### Allowed files

- `src/application/daily_decision_brief_service.py`
- `src/application/daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_renderer.py`
- `docs/AGENT_WIKI.md`
- Gateflow/review artifacts under `docs/gateflow/` and `docs/reviews/`

### Exact changes

1. Extend `_position_view()` with an allowlisted nested `metrics` mapping containing only:
   - `close_mid`;
   - `realized_if_close`;
   - `remaining_annualized_return`.
2. Keep the canonical `daily_decision_brief.v1` schema version unchanged because positions already accept mapping extensions and the change is backward-compatible.
3. Extend the renderer's position view with an optional `details` list.
4. Render the detail line only when:
   - evaluation/quote status is usable;
   - `close_action` is an action that closes the current leg (`close`, `close_put_keep_call`, `sell_call_take_profit`, or `sell_call_salvage`);
   - at least one requested metric is a finite number.
5. Use neutral wording `参考平仓价 ...（mid）` so the line is correct for both buy-to-close short options and sell-to-close long calls.
6. Format money from the brief market (`HK$` for HK, `$` for US) and percentages with one decimal place. Use `预计锁定收益` for a non-negative `realized_if_close`; use `预计平仓损益` for a negative value so a risk stop-loss is never described as profit.
7. If some metrics are absent, render only the available facts. If all are absent, retain the existing one-line position rendering.
8. Update the Daily Decision Brief handbook section to document the close-position detail projection and its advisory/mid-price semantics.

## Invariants

- Do not change close-advice thresholds, tiers, reasons, or action selection.
- Do not change action IDs, material-diff rules, delivery routing, or notification state.
- Ordinary price/yield changes remain non-material and do not independently trigger delivery.
- Do not expose internal IDs, raw paths, broker codes, or arbitrary CSV columns.
- Unavailable/not-evaluable positions retain the existing explicit unavailable status and do not show stale metrics.
- Hold/observe positions remain compact and do not display close details.
- Existing dirty work in the original worktree remains untouched.

## Non-goals

- No bid/ask execution algorithm or fill guarantee.
- No broker order placement or recommended executable limit-order policy.
- No config, runtime artifact, Feishu, position ledger, or production service changes.
- No release, deployment, remote upgrade, or production canary.
- No new schema, config key, abstraction, dependency, or renderer framework.

## Implementation Slice

### Slice A: project and render close details

- Prerequisite: accepted plan review.
- Modify the five allowed source/test/doc files listed above.
- Add service regression proof that close metrics survive the structured position projection.
- Add renderer regressions for:
  - HK actionable close with all three metrics;
  - unavailable/hold rows suppressing details;
  - partial/malformed metrics degrading safely;
  - negative realized P&L using `预计平仓损益` with the correct sign;
  - no internal data leakage.
- Expected completion signal: focused tests pass and generated Markdown matches the requested semantics.
- Stop condition: any evidence that `close_mid`, `realized_if_close`, or `remaining_annualized_return` have different units/meaning from the existing close-advice output, or any need to change strategy policy.

## Validation

Focused:

```bash
./.venv/bin/python -m pytest \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_domain.py \
  tests/test_daily_decision_brief_notification_flow.py
```

Static/diff checks:

```bash
git diff --check
./.venv/bin/python -m compileall -q src/application/daily_decision_brief_service.py src/application/daily_decision_brief_renderer.py
```

Broader regression after focused pass:

```bash
./.venv/bin/python -m pytest tests/test_daily_decision_brief_*.py
```

## Docs Decision

Update `docs/AGENT_WIKI.md` because the user-visible public read model output changes. No CLI or config docs change is needed.

## Residual Risks

- Mid price is advisory and may not be executable; fixed in current slice by explicit `(mid)` wording.
- `realized_if_close` is signed net P&L and can be negative; fixed in current slice through sign-sensitive wording and regression coverage.
- Old persisted briefs without the new metrics keep the former one-line rendering; accepted backward-compatible behavior, classified as fixed in current slice through graceful fallback.
- Production values cannot be canaried without separate authorization; assigned to later operator-controlled release/deployment work.

## Completion Status

- Plan drafted.
- Next gate: plan review.
