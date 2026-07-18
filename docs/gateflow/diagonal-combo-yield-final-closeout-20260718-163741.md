# Gateflow Final Closeout — True Staggered/Diagonal Combo Yield Lifecycle

- Gate: final closeout
- Work unit: true staggered/diagonal Combo Yield opening, holding, exit, assignment, and residual-leg lifecycle
- Branch: `codex/diagonal-combo-yield-lifecycle`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/73`
- Base: `main@0af7adac` (v1.2.411)
- Accepted PR-review integration commit: `c41c639c`
- PR scope-fix commit: `40053b34`
- Artifact path: `docs/gateflow/diagonal-combo-yield-final-closeout-20260718-163741.md`

## What Changed

1. Integrated the work onto current main's released staggered architecture instead of retaining the branch's duplicate vocabulary:
   - `structure_mode=staggered_expiry_pair`;
   - explicit `pair_intent_id`;
   - `funding_put` and `participation_call` roles;
   - existing `src/application/positions/combo_pairing.py` ownership;
   - extracted `domain/domain/assigned_stock.py` ownership.
2. Added quantity-aware option inventory and full group lifecycle classification:
   - active combo;
   - missing Call;
   - residual Call after Put close/expiry/assignment;
   - assigned stock with residual Call;
   - assigned stock only;
   - closed;
   - fail-closed review-required states.
3. Preserved strategy identity through manual/broker intake, ledger event projection, restart, reporting, assignment stock lots, and assigned-stock sales.
4. Enforced one financing cycle per pair intent at broker explicit-intent intake:
   - consumed/closed capacity cannot be reopened under the same group;
   - incoming quantity cannot overmatch the opposite leg;
   - account, symbol, role, structure, and expiry-order conflicts fail closed.
5. Added additive Combo group Close Advice after authoritative leg advice:
   - each leg retains its own tier, reason, action, and exit policy;
   - group action is withheld when identity, quantity, quote, or leg advice evidence is incomplete;
   - residual Calls use their current actual quote and long-Call policy only.
6. Assignment hands off to the assigned-stock workflow. No automatic stock sale, Call exercise, or broker-facing action was added.
7. Put-expiry Call residual value is not predicted. Unknown future value remains unknown/null rather than being treated as zero.

## Put Close / Assignment Semantics

- Normal Put close or Put expiry with an open later-dated Call produces `residual_call`.
- Partial assignment reconciles assigned Put contracts against the open Call quantity and can produce `assigned_stock_with_residual_call`.
- Assignment creates/updates assigned-stock lifecycle evidence; it does not automatically sell shares or exercise the Call.
- Once any group capacity is consumed, the same `pair_intent_id` cannot finance a new Put cycle.

## Verification

- Focused feature plus current-main integration suite: `321 passed`.
- Option Performance and assigned-stock regression suite: `180 passed`.
- Full repository: `2647 passed, 10 skipped`.
- `python3 -m compileall -q domain src`: pass.
- `git diff --check`: pass.
- Dependency graph generate/check: pass; `production_modules=466 cycles=0`.
- US/HK example config validation: pass.
- US/HK example config build dry-runs: pass with `write_applied=false`.
- Draft PR after integration and scope fix: mergeable and `mergeStateStatus=CLEAN`.
- CI at `40053b34`: CodeQL actions/python, CodeQL summary, agent-plugin, and guardrails all pass.

## Docs Decision

Updated:

- `docs/CLOSE_ADVICE_CONTRACT.md`;
- `docs/TOOL_REFERENCE.md`;
- generated dependency graph documents;
- Gateflow plan/implementation/review/fix evidence.

Historical early artifacts retain the original `diagonal` / `sell_put` / `enhancement_call` planning vocabulary as audit evidence. Current-main staggered contracts supersede that vocabulary for implementation and public behavior.

## Finding Status

- `PR-F1` severe stale-base/duplicate-architecture finding: accepted, fixed, re-reviewed **已修复**.
- `PR-F2` unrelated work-unit artifact accidentally included in PR: accepted, removed from PR tracking, local working copy preserved, re-reviewed **已修复**.
- New material findings: none.

## Residual Risks / Owners

1. Broker explicit-intent validation inherits the ledger/intake read-then-write concurrency model; no new lock was introduced. Owner: ledger/intake architecture; later work unit only if concurrent duplicate-fill evidence appears.
2. Group Close Advice is intentionally option-inventory scoped. Assignment-aware truth remains in full-lifecycle reporting. Owner: documented product boundary; no current change required.
3. The separate Combo Yield/Sell Put runtime-decoupling work unit remains untracked locally and is not part of PR #73. Owner: its own future Gateflow.
4. Production config/notification promotion is not part of this work unit and requires a separate CEO decision.

## Safety / External Actions

- No production config was modified.
- No live notification was sent.
- No option-position state, trade event, assigned-stock state, or broker-facing data was written.
- No PR approval, ready-for-review transition, reviewer request, merge, or external comment was performed.

## Issue Link Status

- This work unit was not initiated from a numbered GitHub issue; no issue closing keyword or closeout comment is required.

## Next Entry Point

The user reviews and merges draft PR #73 when satisfied. After merge, any production configuration or notification promotion remains a separate explicit CEO decision.

## Completion Status

- draft-PR-pass achieved.
- final closeout prepared; this docs-only closeout artifact will be pushed and its final CI rechecked before reporting `final closeout pass`.
