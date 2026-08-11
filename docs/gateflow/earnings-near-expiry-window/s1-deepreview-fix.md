# Gateflow Fix Artifact — Earnings Near-Expiry Window S1

- Work unit: `earnings-near-expiry-window`
- Slice: `S1`
- Gate: `fix`
- Date: 2026-08-11
- Branch: `feat/earnings-near-expiry-window`
- Accepted plan commit: `fa5076f2`
- Initial review: `docs/reviews/code-review-20260811-214658.md`
- Re-review: `docs/reviews/code-review-20260811-220613.md`
- Status: fixes implemented, verified, and accepted by clean slice re-review

## Finding decisions and fixes

### CR-S1-01 — accepted — fixed

Combo Funding Put now captures calculation and underwriting decisions, projects the same evidence status used by
Sell Put/Covered Call, preserves completed-plus-partial status through the Combo snapshot index, and makes Daily
Brief consume sealed Combo JSON status as authority. Covered regressions include a pure hard-window gap, a valid
pair with an unresolved sibling, a diagnostic gap plus definitive return rejection, status propagation, snapshot
aggregation, and Brief behavior without CSV authority.

### CR-S1-RR-01 — accepted — fixed

The live Combo cash stage now only attaches cash/capacity facts. It no longer drops rows before Candidate Engine
underwriting, whose existing `hard_capacity_put` rule remains the sole formal capacity gate. The historical
`*_cash_filtered.csv` filename remains as a compatibility audit artifact, but contains the cash-enriched universe
and is never reread as candidate authority. The old Python import name remains an enrichment-only alias for Shadow
Replay compatibility.

Regression coverage proves that a Funding Put with both an earnings evidence gap and zero capacity:

- is evaluated exactly once;
- remains a definitive no-candidate outcome because capacity is conclusive;
- retains `diagnostic_evidence_gap_count=1`;
- has `eligibility_unresolved_count=0`; and
- remains present in the non-authoritative cash audit CSV while absent from the underwritten pair input.

### CR-S1-RR-02 — accepted — fixed

Opening snapshot validation now checks every item in `strategy_results`, `candidate_decisions`,
`ranked_candidates`, and `scope_results` is a mapping before any field access or reconstruction. Malformed
content-hash-consistent JSON therefore raises `OpeningCandidateSnapshotError`, allowing Advice and Daily Brief to
fail closed through their existing snapshot-unavailable boundaries instead of leaking `AttributeError`.

Parameterized coverage exercises all four collections.

### CR-S1-RR-03 — accepted — fixed

`candidate_universe_summary()` now reuses the snapshot module's existing clean no-candidate and benign
not-applicable reason sets. Failed/incomplete/unavailable scopes, completed scopes with a non-clean reason, and
not-applicable scopes with a non-benign reason all enter `affected_scopes`. A genuinely unheld Covered Call remains
complete, while an unavailable Covered Call portfolio context becomes partial and is carried into AI Advice even
when a fully evaluated Sell Put candidate remains actionable. Outcome-unresolved contract scopes also enter the
same projection when a defensive strategy-status correction is the only signal; a diagnostic gap plus another
definitive reject remains complete.

## Focused verification

- `tests/test_combo_yield_steps.py tests/test_opening_candidate_snapshot.py`: `37 passed`
- Final snapshot/Advice/Brief focus: `106 passed`
- Final affected strategy chain: `265 passed`
- Full repository run: `4717 passed, 10 skipped`; the only sandbox failure was the localhost bind test, which passed
  outside the sandbox (`1 passed`)
- Ruff for the two production modules and two direct test modules: pass
- Final Ruff over `domain`, `src`, and `tests`: pass
- compileall and dependency-graph current check (`585` modules, `0` cycles): pass
- `git diff --check`: pass

No live OpenD request, notification, production/runtime write, release, deployment, or upgrade was performed.

Clean re-review: `docs/reviews/code-review-20260811-222733.md` (`Findings: 无`).
