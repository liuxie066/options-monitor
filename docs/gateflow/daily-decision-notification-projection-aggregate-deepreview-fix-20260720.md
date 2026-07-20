# Gateflow Fix Artifact — Aggregate Deepreview

## Gate

- Work unit: `daily-decision-notification-projection`
- Gate: aggregate deepreview fix
- Branch: `codex/daily-decision-notification-a-plus`
- Review artifact: `docs/reviews/code-review-20260720-122657.md`
- Accepted finding: `DR-01`
- Artifact path: `docs/gateflow/daily-decision-notification-projection-aggregate-deepreview-fix-20260720.md`

## Fix Scope

Changed:

- `src/application/daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_renderer.py`

Not changed:

- canonical brief/diff schema;
- lifecycle action IDs;
- material-change decision;
- scheduler cadence or run points;
- persisted delivery identity/pointer;
- provider routing or production config.

## Fix Decisions

- Derived internal candidate change keys from allowlisted structured contract facts, with broker contract identity used only as a non-rendered matching fallback.
- Reordered current candidates so material rows are selected before unchanged rows.
- Allowed changed candidates to exceed the per-strategy soft limit while retaining the existing global item and character hard caps.
- Reused the exact selected candidate rows for the funds section so candidate cards and capacity lines cannot diverge.
- Derived exact changed-position keys from `position_lot_id` when available; structured contract identity is used only when an exact lot identity is unavailable.
- Reordered current positions so exact changed lots are selected before unchanged same-contract siblings, and allowed changed positions to exceed the section soft limit within the global cap.
- Rendered invalidated candidate labels from diff-provided symbol/expiration/strike/option type, never from broker code or parsed internal IDs.
- Kept all internal identity use below the allowlisted user projection boundary.

## Tests Added

- Material candidates below the configured soft limit are promoted, can break that soft limit, and remain synchronized with funds capacity lines.
- A removed Sell Put candidate remains identifiable in the change banner while its broker code stays hidden.
- When multiple lots share the same symbol and contract, the exact changed lot is selected before unchanged siblings without exposing the lot ID.

## Validation

- Renderer focused suite: `15 passed`.
- Combined Daily Brief/domain/service/repository/renderer/Agent/CLI/notification/scheduler/multi-tick/trigger suite: `147 passed`.
- Ruff on changed renderer/test files: passed.
- Dependency graph regenerated and `python3.12 scripts/generate_dependency_graph.py --check` passed with no production cycles.
- `git diff --check`: passed.

## Finding Status

- `DR-01`: **已修复**.

## Residual Risks

- If legacy close-position facts lack both lot identity and structured/broker contract identity, the renderer can only fall back to unchanged order — **accepted compatibility boundary; canonical current producers include these fields and the user message still emits an explicit position-change banner**.
- Real provider delivery and production upgrade remain outside this fix — **assigned to later authorized release/remote steps**.

## Completion Status

- Accepted aggregate finding is fixed and ready for re-review.
