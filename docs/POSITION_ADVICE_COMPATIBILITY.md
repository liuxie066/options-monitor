# Position Advice v1/v2 Compatibility

Position Advice v2 is an additive contract. Close Advice v1 remains readable
and operational throughout rollout and rollback, but one account run has only
one formal advice authority.

## Compatibility matrix

| Shared authority | Producer/artifact | Reader or consumer | Required behavior |
|---|---|---|---|
| no policy, no historical state | v1 | new resolver | First-use default v1 only |
| no policy, historical authority/current/notification state exists | any | any new consumer | `authority_conflict`; no notification and zero v2 action |
| `v1` | v1 | v1 reader | Existing v1 contract unchanged |
| `v1` | v2 artifact retained | v2 reader | v2 unavailable/non-actionable; never reinterpret v1 |
| `v2_shadow` | v1 | v1 reader/Daily Brief | v1 remains formal authority |
| `v2_shadow` | v2 | v2 reader | Readable shadow plan; zero consumer action and no v2 notification |
| `v2_shadow` | no v2 artifact | v2 reader | Unavailable |
| `v2` | v2 from current generation | v2 reader/Daily Brief | Validate identity, policy, hashes, source freshness, and live fingerprint |
| `v2` | v2 from earlier generation | v2 reader/Daily Brief | Stale authority; zero action until a new Account Run publishes |
| `v2` | v1 still generated | v1 reader | Readable for observation only; excluded from Daily Brief and scheduled notification |
| rollback to `v1` | retained v2 artifacts | v2 reader | Non-authoritative/unavailable; no fallback conversion |
| identity-conflicting caller | any | reader/Daily Brief | Conflict even if an old current plan is visible |

An old Daily Brief that does not understand the shared v2 authority must never
consume v2 candidates. Promotion must remain at `v1` until all enabled Account
Run, Daily Brief, and notification consumers support
`position_advice_authority_policy.v1`.

## Rollout sequence

1. Deploy code that can resolve the shared policy while authority remains v1.
2. For each enabled account, dry-run first-use policy creation. The binder must
   prove canonical YAML, all enabled generated market configs, and fresh source
   receipts agree. A blocked attempt returns the per-source hash intent without
   creating shared authority state.
3. Confirm a v1 policy only after reviewing its identity binding and paths.
4. Verify US/HK readers and scheduled notification still select v1.
5. CAS the account to `v2_shadow`.
6. The mandatory daily promotion timer accumulates immutable shadow evidence.
   v2 rows remain non-authoritative and v1 continues to notify. Only canonical
   plans beneath the same runtime `output_runs` tree and bound to the exact
   current shadow generation and current position-fact snapshot contract may
   enter persisted promotion evidence. Integrity-valid older-contract sources
   remain archived but are classified incompatible; an all-incompatible set
   waits for compatible shadow plans instead of failing the timer. The timer
   copies the exact plan/input pairs into a content-addressed gzip archive;
   ordinary output-run cleanup continues without discarding replay evidence.
7. Use `om position-advice ... promotion status --account <account>` to review
   the fixed gate, computed safety counters, deterministic replay results,
   reason distribution, modeled economics, covered families, and
   unknown-delivery state.
8. Only when status reports `ready_for_final_cas=true`, manually dry-run and
   then confirm the v2 CAS with the reported evidence path and expected policy
   hash. Evidence refresh never performs this CAS.
9. Wait for the next successful Account Run under the new generation. The old
   shadow artifact is not promoted in place.
10. Verify the current manifest and Daily Brief select only v2.

No step authorizes release publication, service deployment, broker writes, or
automatic trading.

## Rollback

Rollback is an expected-hash CAS to `v1`:

```bash
./om position-advice --runtime-root <runtime-root> authority set \
  --account <account> \
  --mode v1 \
  --expected-policy-hash <current-policy-hash> \
  --dry-run
```

After review, repeat with `--confirm`. Rollback:

- does not delete v2 artifacts;
- does not downgrade schemas;
- does not rewrite ledger, lifecycle, allocation, or combo identity facts;
- does not remove promotion or notification receipts;
- restores v1 Daily Brief/notification selection on the next run.

An unresolved ambiguous notification does not block emergency rollback to v1.
It remains visible in the immutable change audit and still requires a separate
human resolution.

## Mixed-version failure rules

- Market-local `close_advice.position_advice_authority` is invalid. US and HK
  cannot choose separate modes.
- A policy missing after any current/change/notification state exists is not a
  first-use fallback; it is conflict.
- A malformed policy or change receipt is conflict, never v1 fallback.
- The same normalized label always resolves the same scope. Changing source or
  broker account identity does not create a new namespace.
- An existing label cannot be rebound to a different real portfolio. Use a new
  label; policy deletion is not a migration.
- A real portfolio identity cannot be bound concurrently to two labels/scopes.
- Current manifest, artifact, input, source, authority generation, policy hash,
  and live fingerprint must agree as one set. Mixed facts return zero action.
- `v2_shadow` never sends v2 notification.
- `v2` never merges v1 rows into uncovered v2 strategy families.
- Long Call stays facts-only and non-actionable under v2.

## Notification compatibility

Every new scheduled envelope contains a token for the selected authority. A
missing token fails closed. The token is preserved in delivery-only retry
artifacts, so retries cannot silently re-resolve to a different authority
generation.

Provider outcomes:

| Outcome | Durable state | Retry |
|---|---|---|
| confirmed accepted | `accepted` | Suppressed as duplicate |
| definite failure before delivery | append-only `failed` attempt | Safe retry under the same dedupe reservation |
| timeout or ambiguous delivery | `unknown` | Blocked until human evidence resolves it |

Unknown resolution appends a receipt and never edits the original provider
receipt.

## Artifact retention

The shared control plane and current manifests are persistent. Service cleanup
may delete old output runs only after proving they are not referenced by any
validated current manifest. If a current reference is malformed, missing,
symlinked, absolute, or escapes its run root, cleanup must stop.

Historical v1 and v2 artifacts remain useful for diagnosis, but only the current
shared policy plus validated current manifest can make a v2 row actionable.
