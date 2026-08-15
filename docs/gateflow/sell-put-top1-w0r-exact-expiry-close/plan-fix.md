# Gateflow Plan Fix — Sell Put Top1 W0R Exact-Expiration Close Boundary

- Gate: `fix`
- Work unit: `sell-put-top1-w0r-exact-expiry-close`
- Reviewed artifact: `docs/reviews/plan-review-20260815-192053.md`
- Artifact path: `docs/gateflow/sell-put-top1-w0r-exact-expiry-close/plan-fix.md`
- Status: fix complete; re-review passed

## Finding decisions and fixes

### PR-01 — accepted — 已修复

The plan no longer accepts plain list, tuple, or dictionary history data. It now requires the installed SDK's DataFrame-like success shape, including `code`, `time_key`, and `close` columns, before materializing records. Only a correctly shaped frame with zero records may return `None`; missing columns, plain empty collections, malformed `to_dict`, and invalid rows fail closed.

This preserves the semantic difference between successful source absence and a broken SDK/provider contract without adding a dependency or abstraction. Tests may use pandas, which is already a project dependency, or a minimal DataFrame-like fake.

## Validation

- Goal, method signature, exact-date request, compact return, and single-slice boundary are unchanged.
- No runner, receipt, storage, production configuration, or OpenD operation was added.

## Residual risks

- Live `time_key` output and performance remain assigned to a separately authorized W0R probe.
- Absence timing and classification remain assigned to the future W5 runner.

## Next gate

`plan re-review`
