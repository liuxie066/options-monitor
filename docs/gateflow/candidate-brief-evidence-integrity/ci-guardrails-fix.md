# Gateflow Fix Artifact — PR Guardrails

- Gate: `draft PR / CI fix`
- Work unit: `candidate-brief-evidence-integrity`
- Artifact path: `docs/gateflow/candidate-brief-evidence-integrity/ci-guardrails-fix.md`
- Failed workflow: Guardrails run `31554858924`

## Finding and decision

### CI-GR-01 — accepted — fixed

The first post-review PR run passed lint but failed the sensitive-artifact guard because 13 command examples in this
work unit's Gateflow and DeepReview evidence used a literal personal volume path to the repository virtual
environment. The affected text was audit evidence only; no runtime path, source behavior, configuration, or test
meaning changed.

All 13 occurrences now use the repository-relative public development entry `./.venv/bin/python`. A repository-wide
Ruff run and the same guardrail command used by CI both pass after the replacement.

Final status: `已修复`.

## Validation

```text
./.venv/bin/python scripts/guardrails_check.py \
  --check-doc-wording \
  --check-runtime-config-tracking \
  --check-sensitive-artifacts
```

Result: `[guardrails] OK`.

```text
./.venv/bin/python -m ruff check .
```

Result: `All checks passed!`.

- `git diff --check`: passed.
- Remaining literal personal repository paths in the affected Gateflow/review artifacts: none.

## Documentation decision

This is a portability and sensitive-artifact correction to existing audit documents. No product documentation or
public contract changed.

## Residual risks

- GitHub workflows must be rerun on the pushed fix commit before recording `draft-PR-pass`.
- Product/runtime replay remains assigned to a separately authorized release or upgrade work unit.

## Completion status

The local fix is complete. Current gate / next entry point: `push CI fix -> verify PR checks`.
