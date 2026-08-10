# Gateflow Implementation — S1 Final Input Authority

- Gate: `implementation`
- Work unit: `ai-decision-advice-account-context`
- Slice: `S1`
- Branch: `fix/ai-decision-advice-account-context`
- Accepted plan commit: `aca09b53`
- Status: `accepted; deepreview found no material issues; ready for slice commit`
- DeepReview: `docs/reviews/code-review-20260810-111356.md`

## Scope

S1 strengthens only the AI Decision Advice source/freeze boundary and its pre-freeze evidence symbol consumer:

- `src/application/ai_decision_advice/contexts.py`
- `src/application/ai_decision_advice/orchestration.py`
- `tests/test_ai_decision_advice_contexts.py`
- `tests/test_ai_decision_advice_orchestration.py`

No Candidate Engine, projection formula, prepared artifact producer/loader, config, persistence, prompt, collector,
notification, release, deployment, or runtime state was changed.

## Implementation outcome

1. A ready/degraded strategic portfolio now needs the canonical prepared PM schema, provider
   `portfolio_management`, a nonempty mapped PM account, and passed provider validation before asset rows or PM
   denominators are exposed. A formal schema-valid unavailable artifact preserves its soft-dependency reason.
2. Option rows and prepared FX now require the canonical prepared option schema and `context_source=prepared`, in
   addition to the existing run/account/config, filters.account, trusted snapshot, and row-account checks.
3. `verified_relevant_symbols()` derives evidence scope through the same portfolio and option freeze owners used by
   final input assembly. Orchestration no longer maintains a weaker parallel source test.
4. A rejected source cannot add a symbol to the evidence index or change its hash; valid PM/option symbols remain
   eligible for evidence scope.
5. All failures remain typed unavailable gaps. Candidate snapshot ranking/capacity and the ordinary receipt path are
   unchanged.

## Test evidence

- Red phase: the two source-authority tests and the evidence-hash-equivalence test failed on the accepted base;
  result `3 failed, 25 passed`.
- Green focused set: contexts + orchestration, `28 passed`.
- Expanded S1/relevant baseline: prepared PM, prepared options, contexts, orchestration, projection, and Daily Brief
  notification flow, `103 passed`.
- Ruff on the two production files and two changed test files: pass.
- Python 3.12 compileall for the two production files with pycache redirected to `/tmp`: pass.
- `git diff --check`: pass.

## Residual boundary

The verified symbol helper intentionally reuses the existing freeze computations before evidence is loaded, then
final input assembly repeats the inexpensive deterministic freeze after the evidence index exists. This avoids a new
mutable intermediate authority or a second rule set. S2 owns preparation-to-projection multi-account integration
proof; it is not implemented in this slice.
