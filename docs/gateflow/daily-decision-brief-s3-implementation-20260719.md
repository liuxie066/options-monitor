# Gateflow Implementation — Daily Decision Brief S3

- **Gate**: implementation
- **Work unit**: `daily-decision-brief`
- **Slice**: S3 — renderer and tick delivery integration
- **Date**: 2026-07-19
- **Base**: accepted S2 commit `cd911e18`
- **Status**: implementation complete; ready for code review
- **Artifact path**: `docs/gateflow/daily-decision-brief-s3-implementation-20260719.md`

## Objective and completion signal

Integrate the canonical Daily Decision Brief lifecycle into the existing scheduled notification path while keeping the feature strictly default-off. A single-market scheduled run can render and deliver full/delta/blocked/recovery messages; only provider-confirmed deliveries advance the account+market pointer. `--no-send`, quiet hours, provider failure, local confirmation failure, no-material revisions and multi-market runs do not advance that pointer.

## Changed files

Production:

- `src/application/daily_decision_brief_renderer.py`
- `src/application/tick_notification_flow.py`
- `src/application/multi_account_tick.py`
- `src/application/scheduled_notification.py`
- `src/application/notification_delivery_adapter.py`
- `docs/DEPENDENCY_GRAPH.md`

Focused tests:

- `tests/test_daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_notification_flow.py`
- `tests/test_multi_tick_contract_batch2.py`
- `tests/test_scheduled_notification_multi_account_application.py`

## Implementation decisions

1. **Default-off branch preservation**
   - The existing legacy notification preparation path remains the only path unless `notifications.daily_brief.enabled is True`.
   - Default-off tests prove the Daily Brief assembler is not called and the legacy preparation/finalization path remains active.

2. **One canonical scheduled lifecycle**
   - `multi_account_tick` passes the scheduler markets/decision and authoritative `ran_pipeline_accounts` into `TickNotificationRequest`.
   - The notification flow assembles, persists and renders from the S2 canonical brief; it never parses legacy notification Markdown.

3. **Provider-safe transport identity**
   - S2 logical delivery keys remain the repository/pointer contract.
   - The send boundary deterministically maps a logical key to `om-<32 hex>` via SHA-256 before passing it to providers.
   - All retries reuse the same compact transport key. Confirmation requires the provider result to carry that exact derived key before advancing the logical pointer.

4. **Delivery state transitions**
   - Single-market `full`/`delta` lifecycles become per-account outbound messages.
   - Provider confirmation is necessary but not sufficient: repository confirmation must also succeed before an account is marked notified.
   - Provider success followed by local pointer failure is classified as ambiguous with duplicate risk, is omitted from final `sent_accounts`, and appears in the failure summary/metrics.
   - No-send and quiet-hours paths persist artifacts but skip provider and pointer confirmation.
   - `delivery_kind=none` finishes before route/credential resolution.

5. **Multi-market fail-closed behavior**
   - US/HK account-market artifacts are both persisted with the S2 market-qualified paths.
   - Bundled multi-market outbound is intentionally not invented in this slice: messages are suppressed and no pointer advances.

6. **Bounded Chinese Markdown renderer**
   - Full, blocked, delta and recovery renderers expose P0/P1/P2, Close Advice, whole-contract capacity, three candidate families, rejection categories, events and data gaps.
   - Per-section limits, a 40-item global budget and a 12,000-character message bound prevent unbounded notifications.
   - Position lot/group/leg identity is preserved where present in the lifecycle payload.

## Validation

```text
python3 -m pytest -q tests/test_daily_decision_brief_*.py \
  tests/test_notification_delivery_route.py \
  tests/test_scheduled_notification_application.py \
  tests/test_multi_tick_contract_batch2.py \
  tests/test_scheduled_notification_multi_account_application.py \
  tests/test_tick_notification_perception_flow.py \
  tests/test_multi_account_tick.py
100 passed

python3 -m ruff check <S3 production and focused test files>
All checks passed

python3 -m compileall -q <S3 production and focused test files>
passed

python3 scripts/generate_dependency_graph.py --check
473 production modules, 0 cycles

python3 scripts/guardrails_check.py --check-runtime-config-tracking
OK

git diff --check
passed
```

An initial aggregate command referenced a non-existent `tests/test_notification_delivery_adapter.py`; it was corrected to the repository's actual notification suites listed above. No product failure was hidden.

## Docs decision

- Generated dependency documentation was refreshed because S3 adds the renderer module.
- Public configuration/CLI/Agent usage documentation remains owned by approved S4.
- `VERSION` and `CHANGELOG.md` remain unchanged; this work unit stops at Draft PR and does not release or enable production behavior.

## Residual risks / uncovered areas

- Real Feishu/WeChat provider behavior and the post-send/pre-local-pointer crash window remain assigned to later production canary/observation; no exactly-once outbox is added.
- Multi-market bundled delivery is intentionally unsupported and fails closed; a future product decision owns any combined-message design.
- Historical runs without `daily_decision_brief.v1` remain explicitly unavailable and are assigned to a later migration work unit.
- Delta change projections currently guarantee lot/group identity; `leg_role` appears when carried by the diff payload but the existing domain projection does not guarantee it for every removed action. This is a later schema-display enhancement, not a delivery correctness blocker because `action_id` and `position_lot_id` remain stable.
- No unclassified residual risk.

## Gate transition

- **Current gate**: S3 code review.
- **Next entry point**: run `deepreview` against accepted S2 base `cd911e18`, adjudicate findings, fix/re-review, then create the accepted S3 slice commit only after pass.
