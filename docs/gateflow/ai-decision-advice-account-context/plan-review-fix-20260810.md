# Gateflow Plan Fix — AI Decision Advice Account Context

- Gate: `plan fix`
- Work unit: `ai-decision-advice-account-context`
- Failed review: `docs/reviews/plan-review-20260810-110026.md`
- Plan: `docs/gateflow/ai-decision-advice-account-context/plan-20260810.md`
- Status: `finding accepted; plan revised; re-review passed with bounded risks`
- Accepted re-review: `docs/reviews/plan-review-20260810-110217.md`

## PR-01 — accepted and repaired in the plan

PlanReview proved that final freeze validation alone was insufficient. Before final freeze,
`run_or_reuse_ai_decision_advice()` derives evidence symbols through `_relevant_symbols()`, whose weaker
run/account/config/status checks could admit symbols from a non-PM portfolio or non-prepared option context.
Because the complete `EvidenceIndex.index_hash()` is embedded in frozen evidence, rejected source rows could still
change Advice input identity.

The plan now requires:

1. pre-freeze evidence symbol derivation to reuse the contexts owner's exact prepared-source validation rather than
   duplicate weaker checks in orchestration;
2. invalid PM/option inputs to be equivalent to explicit unavailable inputs in evidence views, index hash, frozen
   external evidence hash, and input bindings;
3. valid PM/option symbols to remain in evidence scope;
4. `orchestration.py` to be included in the affected source and test scope.

No new context layer, source, configuration, persistence state, or fallback was added to the plan.
