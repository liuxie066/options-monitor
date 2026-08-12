# Gateflow Fix Artifact — Slice 2 Code Review

- Work unit: `candidate-brief-evidence-integrity`
- Gate: `fix`
- Slice: `slice-2`
- Review artifact: `docs/reviews/code-review-20260812-085748.md`
- Re-review artifact: `docs/reviews/code-review-20260812-085955.md`
- Status: fix accepted by code re-review
- Artifact path: `docs/gateflow/candidate-brief-evidence-integrity/slice-2-review-fix.md`

## Finding decision and fix

### CR-S2-01 — accepted — fixed

The first CC+LP requirement predicate inspected raw symbol rows even though Daily Brief receives `request.base_cfg`
before profile expansion. A symbol whose `use` template enabled CC+LP could therefore run the strategy while the Brief
skipped its required sealed-snapshot validation.

The predicate now uses the same existing configuration authority as the prefetch path:

1. resolve top-level templates with `resolve_templates_config()`;
2. expand each symbol through `apply_profiles()`, preserving symbol-over-template precedence;
3. derive the effective Combo Yield policy through the existing yield-enhancement helpers;
4. apply the existing current-market and enabled-`cc_lp` checks.

Configuration/profile errors continue to surface rather than being converted into a false “snapshot not required”
result.

Final status: `已修复`.

## Regression coverage

- A current-market symbol using a template that enables CC+LP now requires the sealed CC+LP snapshot and emits
  `cc_lp_snapshot_unavailable` when it is absent.
- A symbol-level `enabled: false` override defeats the template enablement and does not require the snapshot, proving
  merge precedence remains correct.

## Validation

```text
./.venv/bin/python -m pytest -q tests/test_daily_decision_brief_service.py tests/test_daily_decision_brief_renderer.py tests/test_ai_decision_advice_render.py
```

Result: `116 passed`.

```text
./.venv/bin/python -m ruff check src/application/ai_decision_advice/render.py src/application/daily_decision_brief_renderer.py src/application/daily_decision_brief_service.py tests/test_ai_decision_advice_render.py tests/test_daily_decision_brief_renderer.py tests/test_daily_decision_brief_service.py
```

Result: `All checks passed!`.

`git diff --check`: passed.

## Docs decision

No public docs change; this fix aligns existing runtime configuration consumption and introduces no new config field.

## Residual risks

- Aggregate Slice 1 + Slice 2 interaction remains `covered by aggregate DeepReview and clean-clone validation`.
- Production replay remains `assigned to separately authorized release/upgrade verification`.

## Completion status

CR-S2-01 is fixed. The re-review concluded `pass`. Current gate / next entry point: `accepted slice commit`.
