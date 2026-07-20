# Gateflow Final Closeout — HK Daily Decision Brief Canary Correction

- **Work unit**: `daily-decision-brief-canary-correction`
- **Date**: 2026-07-20
- **Branch**: `ops/hk-daily-brief-canary-closeout`
- **Status**: **blocked at real-send authorization gate**
- **Code/release work**: merged and released
- **Stable source-identity and no-send safety checks**: pass
- **Accepted plan's literal P450 content check**: fail

## 1. What changed

The merged correction established one authoritative Daily Brief path:

- Sell Put candidates read only canonical `*_sell_put_candidates_labeled.csv` artifacts.
- Normal Daily Brief assembly does not fall back to raw `*_sell_put_candidates.csv` artifacts.
- CLI and Agent Tool exact reads honor `OM_RUNTIME_ROOT` and read the production runtime root when explicitly set.
- Candidate evidence, active actions, data quality, and actionability remain separate concepts in payloads and rendered Markdown.
- Prepared notification audit records effective render limits, UTF-8 message SHA-256, and character count.

Delivery/repository semantics were intentionally unchanged: no schema bump, no new revision policy, no delivery-key change, and no delivery confirmation change.

## 2. Code and release references

- Feature PR: `#97`
- Feature merge commit: `daf5311ef7c1772bab8ca14c2caa5aec18b87e14`
- Release PR: `#98`
- v1.3.2 release merge commit: `12ccdd6f509c8973c15741382add8f5e7ca5c81d`
- v1.3.2 release commit: `b1ab99c6b843a3871e3c7fc972cf486d4be8b440`
- Subsequent main/release: v1.3.3, commit `35e63f1afa90b4706f9e5ac30924221fdaa919a1` (`#99`)

The first valid production-root Canary ran on v1.3.2. Because the active server later advanced to v1.3.3, a second no-send Canary was run against the exact currently deployed release.

## 3. Negative control retained

The first attempt under:

```text
/tmp/om-daily-brief-canary-20260720T050709Z
```

resolved runtime state under the release directory rather than `/var/lib/options-monitor`. It is not acceptance evidence. Its shadow artifacts were intentionally retained as the runtime-root negative control and were not deleted.

## 4. v1.3.2 production-root Canary

- Evidence directory: `/tmp/om-daily-brief-canary-20260720T051222Z-prod`
- Runtime root: `/var/lib/options-monitor`
- Run ID: `20260720T051404Z-96dc77`
- Execution: `--no-send`, without `--force`
- Audit report SHA-256: `039da72cd434c07138a4b2883dabfd1ffda62a55419c6d030b8d3ae5b30f43a7`

### lx

- Revision: `1`
- Brief ID: `daily-brief-9d162cfc284612c8583f730a`
- Active actions: `6`
- Candidates: Sell Put `3`, Covered Call `2`, Combo Yield `0`
- Data gaps: `0`
- Prepared message SHA-256: `c2fb54a952876ae4042cb650b75131071efe9a2b59e721872875f528886fa568`
- Prepared message characters: `2317`

### sy

- Revision: `1`
- Brief ID: `daily-brief-826cfaf8040082a09fb8c41d`
- Active actions: `6`
- Candidates: Sell Put `3`, Covered Call `3`, Combo Yield `0`
- Data gaps: `0`
- Prepared message SHA-256: `85bbfb99a031c23270fd91a576e4c1dec03f3d604f9e8b7413a08f838f239a3f`
- Prepared message characters: `2447`

For both accounts, canonical revision JSON equaled the run-scoped brief, CLI and Agent Tool returned the same structured brief, and renderer replay reproduced the prepared message SHA and character count.

## 5. v1.3.3 current-production Canary

- Evidence directory: `/tmp/om-daily-brief-canary-20260720T052757Z-v133-prod`
- Active release path at execution: `/home/liuxie/apps/releases/1.3.3`
- Runtime root: `/var/lib/options-monitor`
- Run ID: `20260720T053303Z-595bab`
- Market time: 2026-07-20 13:33 Asia/Hong_Kong
- Execution: `--no-send`, without `--force`
- Audit report SHA-256: `7ade79f428df5ccde3bed86bc3191b2ed317d7e83371bc6024dba9c8e817eed4`

### lx

- Revision: `2`
- Brief ID: `daily-brief-9d162cfc284612c8583f730a`
- Status/actionability: `ready` / `live_actionable`
- Active actions: `5`
- Candidates: Sell Put `3`, Covered Call `2`, Combo Yield `0`
- Data gaps: `0`
- Prepared message SHA-256: `86700f6a38de689a0d64129647c8f1772afa6cf689ccd07ace8fb5bfa68d4078`
- Prepared message characters: `2344`

### sy

- Revision: `2`
- Brief ID: `daily-brief-826cfaf8040082a09fb8c41d`
- Status/actionability: `ready` / `live_actionable`
- Active actions: `6`
- Candidates: Sell Put `3`, Covered Call `3`, Combo Yield `0`
- Data gaps: `0`
- Prepared message SHA-256: `491e53539c50c83949f35148fbf56a8c050651c13bbb114d67b3b439b5b7b624`
- Prepared message characters: `2400`

## 6. Four-surface and renderer result

For both v1.3.3 account revisions:

- canonical immutable revision JSON equals the run-scoped brief JSON;
- CLI exact-revision `brief` equals canonical JSON;
- Agent Tool exact-revision `brief` equals canonical JSON;
- prepared audit, persisted brief, CLI, and Agent agree on account, market, date, run ID, brief ID, and revision;
- active-action count, candidates by family, data-gap count, status, and actionability agree;
- CLI and Agent Markdown hashes agree because both reads retained the same effective actionability;
- v1.3.3 renderer replay with the recorded effective limits reproduces the exact prepared SHA and character count;
- candidate sections contain `候选证据（非行动）`;
- only `actions[state=active]` are counted as executable;
- no candidate lacking accepted capacity becomes an active open-candidate action;
- summary active-action counts equal the actual number of active actions.

## 7. Canonical labeled/raw authority result

The stable identity-based authority check passes for both accounts.

### v1.3.3 lx

- Raw Sell Put contracts: `70`
- Labeled Sell Put contracts: `15`
- Raw-only contracts: `55`
- Raw-only contracts present in candidates/actions: `0`

### v1.3.3 sy

- Raw Sell Put contracts: `70`
- Labeled Sell Put contracts: `15`
- Raw-only contracts: `55`
- Raw-only contracts present in candidates/actions: `0`

All Sell Put candidate and open-candidate action sources end in `_sell_put_candidates_labeled.csv`; no candidate identity falls outside the labeled identity set.

## 8. Safety result

The v1.3.3 Canary passed all recorded safety checks:

```text
reason=no_send
send_attempted_count=0
send_confirmed_count=0
send_failed_count=0
retry_attempt_count=0
ambiguous_send_count=0
duplicate_risk_count=0
provider_message_id=ABSENT
```

Additional evidence:

- production `config.yaml` hash was unchanged before/after;
- production `config.hk.json` hash was unchanged before/after;
- production Daily Brief remains default-off;
- `lx` delivery pointer remained `ABSENT -> ABSENT`;
- `sy` delivery pointer remained `ABSENT -> ABSENT`;
- scheduler `last_notify_utc` and `last_notify_utc_by_account` were unchanged;
- scheduler `last_run_utc_by_account` advanced as expected for a completed no-send scan;
- no production config was replaced by the temporary Canary config.

No real provider delivery was attempted or confirmed.

## 9. Blocking mismatch: hard-coded P450 criterion

The accepted plan's live-content examples state:

- P430/P440 may appear if accepted and labeled;
- rejected/raw-only P450 must not appear.

That literal condition is not true for either production Canary. In the v1.3.3 run, P430, P440, and P450 were all present in the canonical labeled artifact. P450 therefore appeared through the correct labeled source and was not a raw fallback. The identity-based invariant passes, but the literal `P450 absent` assertion fails.

This is a plan/data assumption mismatch, not evidence of raw-only leakage. Nevertheless, section 12.6 of the accepted plan says any mismatch is a stop condition. The real-send gate therefore remains blocked rather than silently rewriting the acceptance rule after execution.

## 10. Findings and remaining risks

| Finding | Status | Owner / next decision |
|---|---|---|
| Canonical labeled-only Sell Put authority | Pass | Product/runtime |
| CLI/Agent `OM_RUNTIME_ROOT` convergence | Pass | Runtime |
| Four-surface exact-revision identity | Pass | Runtime |
| Prepared renderer integrity | Pass | Notification |
| No-send and pointer safety | Pass | Operations |
| Scheduler notified pointer unchanged | Pass | Operations |
| Literal P450 absence criterion | **Fail / blocker** | Product decision |
| Event rendering | Deferred by approved scope | Follow-up work unit |
| Real provider behavior | Not exercised | Requires separate authorization after blocker resolution |

## 11. Recommended resolution

Prefer revising the live Canary content criterion from hard-coded strikes to the stable source-identity rule:

> Every Sell Put candidate/open-candidate action must be a member of the exact run's canonical labeled identity set, and must be disjoint from that run's raw-only identity set.

Keep named P430/P440/P450 cases in deterministic fixtures, where artifact membership is frozen and reproducible. Do not use volatile live market membership as the primary production acceptance oracle.

If the CEO instead intends P450 to be rejected as a business-policy requirement regardless of labeling, this is a separate strategy/label-policy defect and requires root-cause investigation before another Canary.

## 12. Gate decision and next entry point

- Code/release implementation: complete.
- Current v1.3.3 no-send technical/safety evidence: pass.
- Gateflow final closeout: **blocked**, because the accepted literal content criterion does not pass.
- Real sending: **not authorized**.

Next entry point requires one explicit product decision:

1. approve a narrow plan amendment replacing the volatile P450 live assertion with the identity-based acceptance rule, followed by final plan review and closeout re-evaluation; or
2. preserve the P450 requirement and open a new investigation into why P450 is labeled/active.
