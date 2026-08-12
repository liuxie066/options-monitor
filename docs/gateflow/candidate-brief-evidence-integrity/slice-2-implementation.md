# Gateflow Implementation Artifact — Slice 2

- Work unit: `candidate-brief-evidence-integrity`
- Gate: `implementation`
- Slice: `slice-2` — Daily Brief evidence consumption and truthful fallback copy
- Base commit: `0893f5e0`
- Status: accepted after code review/fix/re-review
- Artifact path: `docs/gateflow/candidate-brief-evidence-integrity/slice-2-implementation.md`
- Failed review: `docs/reviews/code-review-20260812-085748.md`
- Accepted re-review: `docs/reviews/code-review-20260812-085955.md`

## Objective and outcome

Consume the corrected sealed opening-candidate evidence without inventing missing-data warnings or claiming that a
raw ranking follows when no candidates exist.

Implemented outcome:

- required-data prefetch status `fetched` is treated as a successful terminal state;
- the CC+LP snapshot is required only when an enabled `cc_lp` policy exists for a symbol in the Brief's current
  market;
- `term_matched_rv_unavailable` survives the Brief data-gap projection and renders as a specific Chinese reminder;
- unavailable AI advice uses a no-ranking message for an empty strategy family and retains the existing raw-ranking
  fallback for a family that has candidates;
- mapping-shaped normalized briefs and flat user-view candidate collections remain supported.

## Changed files

- `src/application/daily_decision_brief_service.py`
- `src/application/daily_decision_brief_renderer.py`
- `src/application/ai_decision_advice/render.py`
- `tests/test_daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_renderer.py`
- `tests/test_ai_decision_advice_render.py`
- this artifact

## Decisions and invariants

- Template references are expanded through the existing `resolve_templates_config()` and `apply_profiles()` path;
  strategy enablement is then derived through `resolve_yield_enhancement_cfg()` and
  `derive_yield_enhancement_policy()`. The Brief does not recreate merge or policy defaults.
- CC+LP dependency checks are market-qualified and ignore disabled, SP+LC, and other-market symbols.
- No new snapshot schema, public config key, strategy state, or candidate-ranking path is introduced.
- Unknown or unallowlisted partial-data reasons keep the existing generic warning.
- Direct callers of `render_family_advice_lines()` retain the old unavailable fallback unless they explicitly provide
  candidate-presence facts; the Daily Brief renderer supplies those facts from the actual rendered family.

## Validation

Command:

```text
./.venv/bin/python -m pytest -q tests/test_daily_decision_brief_service.py tests/test_daily_decision_brief_renderer.py tests/test_ai_decision_advice_render.py
```

Initial result after correcting one generator-expression syntax error and one test import: `114 passed`. Result after
the CR-S2-01 template-resolution fix: `116 passed`.

Assertions include:

- `GOOGL` and `NVDA` prefetch rows with status `fetched` create no source data gap;
- enabled SP+LC, disabled CC+LP, and other-market CC+LP do not require a CC+LP snapshot;
- enabled current-market CC+LP fails closed when its sealed snapshot is absent;
- template-enabled current-market CC+LP has the same requirement, while an inline symbol disable overrides the
  template and removes it;
- a concrete term-matched RV reason reaches the rendered reminder while preserving a valid candidate from another
  strategy scope;
- unavailable AI advice chooses copy independently for an empty Sell Put family and a populated Covered Call family.

`git diff --check`: passed.

## Docs decision

No public docs change. The changes correct internal evidence consumption and existing user-facing copy without
changing commands, config, or schemas; Gateflow artifacts provide the audit trail.

## Residual risks and uncovered areas

- Aggregate interaction between Slice 1 evidence classification and Slice 2 rendering: `covered by aggregate
  DeepReview and clean-clone validation`.
- Production artifacts and scheduled delivery: `assigned to separately authorized release/upgrade verification`.

## Completion status

Slice 2 implementation and review loop are accepted. Current gate / next entry point: `accepted slice commit`.
