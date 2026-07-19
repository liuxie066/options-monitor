# Plan Amendment — Daily Decision Brief Full Delivery Idempotency

- **Gate**: S2 re-review blocking-question resolution
- **Work unit**: `daily-decision-brief`
- **Slice**: S2 — structured assembler and persistence lifecycle
- **Date**: 2026-07-19
- **Decision owner**: user
- **Decision**: accepted option A — semantic-content full key
- **Status**: implemented; pending final S2 re-review
- **Artifact path**: `docs/gateflow/daily-decision-brief-plan-amendment-20260719.md`

## Blocking scenario

The accepted plan used one fixed full key per market/date/account:

```text
daily-brief:<market>:<date>:<account>:full
```

That key is unsafe when the provider delivers revision 0, the process crashes before the local delivery pointer is written, and revision 1 has different content. Retrying revision 1 with the same provider key can be absorbed as a duplicate of revision 0; if revision 1 is then confirmed locally, the material change can be lost permanently.

Direct production-path evidence:

- `notification_delivery_adapter.send_feishu_app_message()` forwards `idempotency_key` unchanged.
- `feishu_bot.send_text_message()` writes that value to the Feishu `uuid` request field.
- S2 intentionally prepares the latest full revision while no confirmed full pointer exists.

## Confirmed contract amendment

Full delivery key becomes:

```text
daily-brief:<market>:<date>:<account>:full:<semantic-brief-digest>
```

The semantic digest:

- excludes revision, run ID, generated-at/data-as-of timestamps;
- excludes strategy-summary prose, action title/reason/source, and source-artifact/provenance noise;
- preserves canonical status, actionability, validity, actions, positions, capacity, candidates, rejections, events and data gaps, including numeric content;
- is persisted in the immutable run-scoped full diff and verified again before delivery confirmation.

State-machine consequences:

1. Same semantic full content after a crash reuses the same provider key.
2. Changed semantic full content receives a new key and cannot be mistaken for the previously delivered revision.
3. Confirmation with an old or tampered full key fails closed and cannot advance the pointer.
4. No outbox, queue, database, scheduler or second delivery stack is introduced.

## Scope and tests

Allowed S2 files remain unchanged. Required regressions:

- volatile/audit/display-only changes reuse the full key;
- semantic/numeric content changes produce a different full key;
- an old full key cannot confirm a newer changed revision;
- a tampered immutable full semantic digest fails closed;
- all existing revision, delta, read, assembler and domain tests remain green.

## Residual risks

- Provider key format/length and provider-side idempotency behavior remain mandatory S3 integration-review evidence.
- Providers without idempotency can still duplicate in the post-send/pre-pointer crash window; this remains a classified production-observation risk.
- No unclassified S2 residual risk after this amendment.

## Gate transition

- **Current gate**: S2 fix/re-review.
- **Next entry point**: record CR-S2-6 as fixed, run focused validation, execute final deepreview re-review, and accept S2 only if it passes.
