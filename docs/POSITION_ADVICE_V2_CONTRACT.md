# Position Advice v2 Contract

Position Advice v2 is a portfolio-level advisory system for existing option
positions. It compares hold, roll, replace, reallocate, and manual-review
outcomes from one coherent account snapshot. It never submits orders, changes
runtime configuration, sends a notification by itself, or treats an advice row
as execution authorization.

The existing Close Advice v1 contract remains independent. The shared authority
policy decides which contract may enter Daily Brief and scheduled notification.
See [Position Advice Compatibility](POSITION_ADVICE_COMPATIBILITY.md) for the
rollout and rollback matrix.

## Public contracts

| Contract | Purpose |
|---|---|
| `position_advice_source_receipt.v1` | Immutable producer-owned source completion receipt |
| `position_advice_source_manifest.v2` | One account-run manifest of adopted source bytes |
| `decision_state_snapshot.v2` | Coherent ledger events, lots, lifecycle, allocation, and identity facts |
| `decision_state_fingerprint.v2` | Canonical hash of all decision-relevant ledger facts |
| `position_advice_input.v2` | Immutable input bundle bound to source, identity, authority, and fingerprint |
| `position_advice.output.v2` | Immutable portfolio plan |
| `account_decision_current.v2` | Atomic pointer to the one current complete plan for a portfolio scope |
| `position_advice_read.output.v2` | Revalidated pure-read response |
| `position_advice_authority_policy.v1` | Shared v1/v2 authority policy |
| `position_advice_promotion_evidence.v1` | Immutable shadow evidence aggregation |
| `position_advice_promotion_gate.v1` | Fixed go/no-go evaluation |

`position_advice.output.v2` is additive. It does not append authoritative v2
fields to `close_advice.csv`, and the v2 reader never interprets a v1 artifact
as a v2 plan.

## Authority and identity

The durable scope is derived only from the normalized account label:

```text
portfolio_scope_id =
  sha256(options-monitor.position-advice.scope.v2 + normalized_account_label)
```

The label is trimmed, lowercased, and treated as a deployment-local durable
identifier. Portfolio source aliases and broker account IDs do not create a
different scope. After loading that scope, every caller must match the policy's
normalized portfolio source and `portfolio_account_identity_hash`.

An identity mismatch is `authority_conflict`. It prevents current publication,
returns zero actionable v2 rows, and blocks both v1 and v2 notification for the
conflicting caller. Deleting a policy or reusing an old label for another real
account is not a supported rebind workflow; create a new account label instead.

First policy creation requires `--expected-policy-hash absent`. The binder
compares the canonical `config.yaml`, every enabled market's generated runtime
view, and a fresh portfolio source receipt. It also scans all existing policies
under a global exclusive lock so one real portfolio identity cannot bind to two
labels/scopes. A rejected binding writes no control-plane state; the command
returns a failed intent evidence object containing the authoring config hash,
each available generated config hash, and each observed portfolio receipt hash.
A successful first-use apply persists the complete binding object under
`identity_bindings/` before writing the change receipt, and the receipt binds
that object by hash.

Authority modes:

| Mode | Formal advice authority | v2 generation | Scheduled notification |
|---|---|---|---|
| `v1` | Close Advice v1 | May be absent or retained for observation | v1 only |
| `v2_shadow` | Close Advice v1 | Generated and readable, always non-actionable to consumers | v1 only |
| `v2` | Position Advice v2 | Generated under the current generation and policy hash | v2 only |

Every change is a human-only expected-hash CAS. Dry-run is the default and apply
requires `--confirm`. Authority is not a market-local config field and is not an
Agent write tool.

## Source evidence

Source ownership is payload-first, receipt-last:

| Source | Required evidence |
|---|---|
| Quote | Actual observation time, fetch plan/policy, payload hash |
| Candidate decisions | Normalized inputs plus opening and invariant decisions before legacy capacity filtering |
| Portfolio/holdings | Broker/source identity and original observation time |
| Cash capacity/share coverage | Dependencies on portfolio, ledger fingerprint, FX, and stable pool authority |
| FX | Provider observation time; cache reads preserve it |
| Ledger | Same-snapshot raw events/lots plus reprojected trust result |

The input builder validates receipt completion, payload hashes, identity,
dependency closure, expiry, and run scope. Shared quote or FX bytes are copied
into the account run; symlinks, hardlinks, mutable cache references, mtime, cache
read time, and builder time are not freshness evidence.

Fixed freshness policy:

- quote, candidate, portfolio, capacity, and holdings facts: 1,800 seconds;
- FX facts: 86,400 seconds;
- non-FX input snapshot skew: at most 300 seconds.

These are versioned contract values, not runtime tuning knobs. Missing,
incomplete, stale, skewed, or changed-during-adoption evidence makes dependent
actions non-evaluable.

## Lifecycle and combo identity

Expiration alone never proves assignment, exercise, called-away, or expiry
closure. Discovery creates deterministic lifecycle cases after the market-local
observation boundary. Silence remains pending; after 72 elapsed hours it becomes
review-required, not automatically expired.

Lifecycle evidence is quantity-aware and append-only. Partial evidence can
resolve part of a lot, duplicate evidence is idempotent, ambiguous quantity
binding remains conflict, and terminal events, projection updates, and
allocation rows commit atomically.

`combo_identity.v2` is insert-only and binds group identity to canonical opening
facts. A legacy group without verified identity is `identity_unverified`.
Active Combo advice is synthesized once at group level; individual legs cannot
issue conflicting authoritative actions. Partial close or assignment cannot
revive the old group. A residual funding Put is treated as a Put, while a Long
Call remains facts-only in v2 because no forward-value action model is approved.

The operator reconciliation surface is dry-run by default:

```bash
./om option-positions lifecycle reconcile \
  --runtime-root <runtime-root> \
  --account lx \
  --format json
```

Supplying `--evidence-json <path>` or applying writes follows the command's
normal option-position confirmation gate (`--confirm` or `--yes`). Close Advice,
Position Advice read, and Daily Brief never initiate ledger reconciliation.

## Economics and allocator

The approved economic model is `observable_carry.v1`. It compares observable
daily carry over an explicit horizon after current close friction. It is not a
forecast EV model and does not label modeled uplift as realized PnL.

Replacement candidates must pass the same normalized non-resource invariant
pipeline as opening candidates. Only an opening-accepted candidate or a
candidate rejected solely by `hard_capacity_put` / `hard_capacity_call` may
reach the release-aware allocator. Any other invariant, policy, input, or hash
drift remains rejected.

The account-level deterministic allocator reserves typed resource pools:

- base-CNY uncommitted cash headroom;
- same-symbol eligible covered shares;
- candidate contract quantity.

Cash and shares never substitute for each other, shares from different symbols
never merge, candidate quantity and released resources cannot be double-used,
and a multi-resource proposal reserves atomically. Efficiencies from different
pool types are not compared against each other.

Recommendation values:

| Recommendation | Actionable |
|---|---|
| `hold` | No |
| `roll` | Yes, only supported standalone/residual same-symbol short options |
| `replace` | Yes |
| `reallocate` | Yes |
| `review` | Yes, as an explicit human fact-review request |
| `not_evaluable` | No |
| `none` | No |

Actionability has two independent fields:

- `model_trade_actionable`: the model selected a supported trade proposal;
- `human_review_required`: a lifecycle or identity fact needs an explicit
  operator review, even though no trade is authorized.

`model_actionable` remains a compatibility alias for
`model_trade_actionable`. A `review` row is therefore
`model_trade_actionable=false`, `human_review_required=true`, and is rendered
as a P0 human operation in Daily Brief under `v1`, `v2_shadow`, and `v2`.

`settlement_pending` and `partially_resolved` lifecycle states cannot produce
an option action.

## Publication and reader

Publication order is:

1. publish the completed run-specific source manifest;
2. build immutable input from decision fingerprint A;
3. calculate the portfolio plan;
4. reread and validate every source byte and manifest;
5. generate decision fingerprint B;
6. require A = B and re-resolve the same authority generation/policy;
7. atomically replace `account_decision_current.v2.json`.

Run artifact paths in the current manifest are relative, contained by the
referenced account-run root, and cannot traverse symlinks. A crash before the
pointer switch leaves the old complete plan current. A malformed pointer,
missing run, hash mismatch, or containment failure is fail-closed.

`position_advice_read` is a pure-read Tool Gateway surface:

```bash
./om-agent run --tool position_advice_read \
  --input-json '{"config_key":"us","account":"lx"}'
```

The reader holds global shared then scope shared locks, validates the current
manifest and all artifact hashes, compares authority/identity, checks per-source
expiry, and performs complete ledger reprojection A/B. If the snapshot changes,
it retries the whole read once. A second change, stale source, projection drift,
superseded requested plan, or authority mismatch yields zero actionable rows.
It does not refresh broker or quote data.

## Daily Brief and notification authority

Daily Brief resolves one advice contract for an account run; it never merges v1
and v2 actions. `v2_shadow` may appear only as a non-actionable preview.

Every scheduled send carries a notification-authority token bound to account
scope, portfolio identity, selected contract, authority generation/policy,
account run, and channel. The flow holds global shared then scope shared locks
from final resolution through bounded provider completion. The dedupe key is:

```text
portfolio_scope_id + authority_generation + account_run_id + channel
```

Accepted delivery is strongly deduplicated. A definite failed attempt can be
retried under the same reservation with a new append-only attempt receipt.
Timeout or ambiguous delivery becomes durable `unknown`; it cannot be resent or
used for promotion until an operator appends a delivery resolution receipt.
Emergency rollback to v1 remains allowed and records the sorted outstanding
notification receipt IDs in its immutable authority change receipt.

## Shadow promotion

Promotion is per account and only from `v2_shadow`. Evidence uses immutable,
fresh, trusted shadow plans and deduplicates repeated scheduler runs by decision
state, source facts, candidate, and economic inputs. Market sessions use the
exchange-local session date. Publishing promotion evidence accepts only
canonical, non-symlinked plans at
`output_runs/<run>/accounts/<account>/position_advice.v2.json` beneath the same
runtime root. Republished producer receipts do not create a new opportunity
when their stable source facts are unchanged.

The mandatory maintenance timer refreshes promotion evidence once per day at
05:15 Beijing time. It discovers every canonical plan bound to the current
account `v2_shadow` generation, copies each exact plan and immutable input into
a content-addressed gzip archive under the promotion control plane, calculates
the six safety counters from those archived sources, runs the seven bounded
deterministic replay fixtures through production domain functions, evaluates
the fixed gate, and publishes immutable evidence and gate artifacts. Repeating
the refresh for an unchanged plan set is idempotent. Dry-run does not create the
archive. The timer never changes authority, sends a notification, writes the
ledger, or trades.

The gate accepts safety and replay results only when their independently hashed
automatic reports bind the exact same source-plan hash set and match the
top-level counters. Caller-supplied zero counters or `true` fixture flags
without those reports remain `insufficient_evidence`.

The fixed promotion gate (`position_advice_promotion_gate.v1`) requires:

- all safety counters equal zero;
- at least 10 distinct market sessions spanning at least 14 elapsed days;
- at least 30 unique eligible evaluations;
- at least 10 unique replacement opportunities;
- at least 5 selected proposals, including 3 capacity-deferred proposals;
- per-covered-family minimum opportunity and selection coverage;
- all critical deterministic fixtures;
- complete receipts for selected actionable opportunities;
- positive aggregate modeled daily carry uplift, horizon improvement, typed
  pool efficiencies, and median selected proposal efficiency.

No opportunity, no selected proposal, unknown safety/economics, an uncovered
strategy family, or incomplete evidence is `insufficient_evidence`. Long Call
is not a promotable family. Without canonical executed-event binding, realized
outcome remains `unknown`.

## Human operations

First-use v1 policy:

```bash
./om position-advice --runtime-root <runtime-root> authority set \
  --account lx \
  --mode v1 \
  --expected-policy-hash absent \
  --config-yaml <runtime-root>/config.yaml \
  --dry-run
```

Apply only after reviewing the complete plan:

```bash
./om position-advice --runtime-root <runtime-root> authority set \
  --account lx \
  --mode v1 \
  --expected-policy-hash absent \
  --config-yaml <runtime-root>/config.yaml \
  --confirm
```

Later shadow, promotion, and rollback use the current policy hash:

```bash
./om position-advice --runtime-root <runtime-root> authority set \
  --account lx \
  --mode v2_shadow \
  --expected-policy-hash <current-policy-hash> \
  --dry-run

./om position-advice --runtime-root <runtime-root> promotion refresh \
  --accounts lx sy \
  --dry-run

./om position-advice --runtime-root <runtime-root> promotion status \
  --account lx

./om position-advice --runtime-root <runtime-root> authority set \
  --account lx \
  --mode v2 \
  --expected-policy-hash <current-policy-hash> \
  --evidence <promotion-evidence.json> \
  --dry-run

./om position-advice --runtime-root <runtime-root> authority set \
  --account lx \
  --mode v1 \
  --expected-policy-hash <current-policy-hash> \
  --dry-run
```

Resolve an ambiguous delivery only from external evidence:

```bash
./om position-advice --runtime-root <runtime-root> authority resolve-notification \
  --account lx \
  --receipt-id <receipt-id> \
  --resolution delivered \
  --evidence <delivery-evidence.json> \
  --dry-run
```

`promotion refresh` is also dry-run by default; `--confirm` publishes only the
immutable evidence and gate. `promotion status` reports the latest valid
artifact bound to the current policy and, only after a passing gate, prints the
exact evidence path and expected policy hash for the final CAS. It does not
perform that CAS.

Replace `--dry-run` with `--confirm` only after review. These commands do not
trade, publish a release, deploy, or change production services. Authority
changes and notification resolution mutate the shared control plane;
promotion refresh only appends immutable source, evidence, and gate artifacts.

## Persistence and operating boundary

Current manifests, authority policies, successful first-use identity bindings,
change receipts, compressed promotion sources, promotion evidence, promotion
gates, notification receipts, and human notification resolutions live under:

```text
output_shared/state/position_advice/<portfolio_scope_id>/
```

They are persistent control-plane state and are never output-run cleanup
candidates. The content-addressed gzip archive stores each exact plan/input pair
needed to replay the full observation window without retaining its
multi-megabyte account run. Cleanup therefore continues ordinary output-run
retention and separately protects every run referenced by a validated current
manifest. It fails closed on malformed or escaping current references. After an
authority change leaves `v2_shadow`, the immutable source archive and promotion
evidence remain authoritative.

v2 assumes US/HK runners share one `output_shared` filesystem and POSIX locks.
Multi-host active-active authority requires a separate distributed-coordination
design and is not supported by this contract.
