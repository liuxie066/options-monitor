# Gateflow Plan Review Fix — Copilot Option Performance MTD

- Work unit: `copilot-option-performance-mtd`
- Gate: `plan review fix`
- Date: 2026-07-23
- Initial review: `docs/reviews/plan-review-20260723-164351.md`
- First re-review: `docs/reviews/plan-review-20260723-164617.md`
- Revised plan: `docs/gateflow/copilot-option-performance-mtd/plan.md`
- Status: fixed; pending re-review

## PR-01 — accepted — fixed

The plan no longer performs generic final-payload deletion of `None` or empty strings.

- `option_performance_report.safe_default_input` will contain only real defaults.
- schema/tool descriptions will not inject safe `default:null`.
- explicit static/model invalid values remain visible to the execution schema and fail closed.
- the option-performance normalizer removes only period fields irrelevant to the selected
  discriminator.
- tests now require `account=""`, `config_key=""`, and invalid current-period fields to fail.

This prevents a malformed request from silently widening to all accounts or switching to the
default US market.

## PR-02 — accepted — fixed

The plan no longer claims the host can prove account-argument provenance.

- no account argument keeps the canonical all-account aggregation;
- an account argument remains an explicit tool filter;
- the renderer must always show the actual scope;
- prompt/eval check scope visibility;
- no keyword/account-intent parser is introduced.

Strong “current-message-only” account provenance is recorded as a separate residual product
boundary because the existing scene contract carries no account provenance.

## Initial review readiness

The two initial findings were incorporated without widening the implementation beyond the
confirmed work unit; the first re-review then identified PR-03.

## PR-03 — accepted — fixed

The Copilot adapter will no longer let `period="mtd"` safe default act as discriminator
provenance for option performance.

- explicit static/model/scene inputs are normalized before safe defaults are applied;
- the public Agent manifest keeps its existing `period="mtd"` safe default;
- a truly empty period request still receives MTD;
- `month` without an explicit period remains visible beside the later default and is rejected
  as ambiguous;
- only an explicitly supplied valid period allows the Copilot normalizer to remove irrelevant
  period fields;
- unknown periods and invalid relevant values remain fail closed.

This avoids mistaking a safe default for discriminator provenance and silently querying the
wrong period.

## Final review readiness

PR-01, PR-02, and PR-03 are incorporated. The second re-review passed with bounded residual
risks; no open plan blocker remains.
