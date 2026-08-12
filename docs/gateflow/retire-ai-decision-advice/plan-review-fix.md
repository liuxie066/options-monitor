# Gateflow Plan Review Fix — Retire AI Decision Advice

- Work unit: `retire-ai-decision-advice`
- Gate: `plan review fix`
- Date: 2026-08-12
- Initial review: `docs/reviews/plan-review-20260812-112642.md`
- Revised plan: `docs/gateflow/retire-ai-decision-advice/plan.md`
- Artifact path: `docs/gateflow/retire-ai-decision-advice/plan-review-fix.md`
- Status: fixes complete; pending PlanReview re-review

## Finding decisions and fixes

### PR-01 — accepted — 已修复

Slice 1 now retains the existing private Brief assembler handoff parameters but ignores their Advice-only values. This
keeps the current Tick caller runnable. Slice 2 owns atomic removal of `_advice_handoff_for_account()`, request fields,
and the corresponding assembler parameters, and therefore includes `daily_decision_brief_service.py` in its allowed
files.

### PR-02 — accepted — 已修复

The current Daily Brief normalizer now has one required contract: it strips both retired keys even when supplied a
legacy mapping. Historical integrity no longer depends on retired fields surviving normalization. The compatible
digest helper reconstructs an overlay-era candidate only from exact raw key/value pairs attached to the same
normalized core; repository files remain immutable and no consumer-specific strip list is required.

### PR-03 — accepted — 已修复

The frozen-retry classifier is explicitly repository-owned. It reuses strict raw revision, identity, and digest
validation, covers fallback text and normalized card transport, returns only `clean` or
`legacy_ai_payload_retired`, and runs before either outbound map is populated. Notification code does not construct
storage paths or parse raw Brief files.

### PR-04 — accepted — 已修复

The plan now requires targeted retired-key errors at root YAML, market YAML, and runtime JSON entry points, with a
nearby misspelling regression preserving the generic unknown-key contract.

## Validation of plan fix

- Each accepted finding maps to an exact contract, allowed file owner, and regression in the revised plan.
- The two-slice count remains unchanged; the fix only repairs sequencing and ownership.
- The fix adds no schema version, storage object, migration, service, provider, or replacement recommendation layer.

## Documentation decision

Only Gateflow plan/review artifacts changed at this gate. Product documentation remains owned by Slice 2.

## Residual risks and uncovered areas

- Installed collector services after source retirement — `assigned to later work unit` requiring separately authorized
  production reconciliation.
- Historical Advice/Evidence/Brief disk usage — `assigned to later work unit` requiring destructive-data approval.
- Third-party callers outside this repository — `assigned to later work unit`; repository-private Python call surfaces
  are not a promised external API, while this work unit proves all in-repository callers before deletion.
- Provider deliveries already accepted before retirement — `assigned to later work unit`; no replay or retraction is
  authorized.

## Next gate

PlanReview re-review. The plan cannot be committed as accepted until all four findings are independently shown fixed
and no new blocker is found.
