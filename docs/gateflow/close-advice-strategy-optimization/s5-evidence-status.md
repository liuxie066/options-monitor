# Gateflow S5 Evidence Status — Close Advice Strategy Optimization

- Gate: `evidence`
- Work unit: `close-advice-strategy-optimization`
- Date: `2026-07-23`
- Status: `hard stop active; remain shadow-only`
- Production policy authority: `P0_current`
- Selected promotion policy: none
- CEO production approval: not granted

## Outcome

S1-S4 are implemented and locally accepted, but the repository does not yet have an admissible close-decision evidence cohort. No P1/P2/P3 policy can be selected from the current data without fabricating parity or forward outcomes. S6 production promotion must not start.

## Read-only evidence inventory

The existing Shadow Replay root contains 42 candidate-analysis datasets:

- `close_decision_episodes.jsonl`: 0 files
- `close_decision_marks.jsonl`: 0 files
- `close_decision_outcomes.jsonl`: 0 files
- datasets exposing `close_decision_readiness`: 0

The public status surface reports:

- 42 total datasets;
- 40 `ready_for_settlement` and 2 `not_ready` under the pre-existing candidate facet;
- 0 datasets eligible for close-policy paired analysis.

Those candidate statuses are not close-policy readiness and must not be used as a proxy for it.

## Historical archive assessment

The read-only remote archive spans `20260601T014013Z-667d97` through `20260717T170016Z-c272b0` and contains:

| Artifact | Count |
|---|---:|
| `close_advice.csv` | 210 |
| `option_positions_context.json` | 210 |
| `audit_events.jsonl` | 105 |
| `close_advice_reallocation_shadow.csv` | 106 |
| close-advice files containing `position_lot_id` | 106 |
| close-advice files containing `policy_version` | 0 |

These archives predate the S1 close-decision contract. They do not contain the required `policy_version`, `recommendation_state`, `decision_basis`, and `decision_evidence_status` fields. The close facet deliberately fails closed when those fields are absent because P0 parity cannot otherwise be demonstrated. The archive may remain useful as diagnostic context, but it is not admissible promotion evidence and will not be rewritten or silently backfilled.

### Admissibility probe

The newest archived run, `20260717T170016Z-c272b0`, was passed to the public
dataset builder with `--include-close-decisions` and an isolated temporary
output target. Capture stopped before creating the target and returned:

```text
formal close policy fields missing
(policy_version,recommendation_state,decision_basis,decision_evidence_status)
```

The probe output directory remained absent. This confirms that the production
capture path enforces the same no-backfill decision as this inventory.

## Dry-run data plan

The public command was run with `--source local`, `--max-datasets 3`, and without `--write`.

- plan actions found: 40
- planned: 3
- executed: 0
- skipped by limit: 37
- errors: 0
- receipt written: false
- OpenD read: false
- persistent write: false

All 40 actions belong to candidate mark maintenance. The plan cannot create close-decision episodes or their future outcomes.

## Readiness gap

The following evidence must be collected prospectively from runs that emit the S1 contract:

1. Capture immutable close-decision episodes with P0 parity and P1/P2/P3 projections.
2. Collect exact 1/3/7/14-day and expiration marks using the same position identity and point-in-time provenance.
3. Derive terminal lifecycle outcomes and paired, fee-and-slippage-aware policy outcomes.
4. Reach at least 30 settled episodes overall, at least 10 usable episodes in every profile/family segment proposed for promotion, and at least 80% usable evidence within each such segment.
5. Run the S4 paired analysis and present the segment-level trade-offs to the CEO without an automatic winner.

Any OpenD collection with `--write`, local evidence write that calls external data, deployment, or production-policy promotion requires its own explicit approval.

## Gate decision

- S5 completion signal: not met
- Promotion decision artifact: not generated
- S6 entry: prohibited
- Current action: keep P0 as formal production authority and keep P1/P2/P3 shadow-only
- Next authorized checkpoint: publish/deploy the additive S1-S4 instrumentation, then begin explicitly approved prospective evidence collection

This stop is intentional. In particular, the desired P2 behavior—an underwriting-profile medium signal with roughly 7% remaining annualized return, valid thesis, complete evidence, and unchanged willingness resolves to `hold`—is implemented and tested only as a shadow policy until the evidence gate and CEO decision are satisfied.
