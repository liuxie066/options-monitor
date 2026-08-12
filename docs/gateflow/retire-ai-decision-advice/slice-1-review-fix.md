# Gateflow Fix Artifact — Slice 1 DeepReview

- Work unit: `retire-ai-decision-advice`
- Gate: Slice 1 `fix`
- Review artifact: `docs/reviews/code-review-20260812-120834.md`
- Status: fixes complete; pending Slice 1 re-review
- Artifact path: `docs/gateflow/retire-ai-decision-advice/slice-1-review-fix.md`

## Finding decisions and fixes

### DR-S1-01 — accepted — fixed

`legacy_ai_payload_retired` is now a terminal local delivery blocker rather than a successful no-message outcome.

- A preparation with no clean accounts finalizes with error outcome, return code 2, a terminal idempotency failure,
  guard failure, and the stable blocker code.
- A mixed-account preparation still sends clean accounts, records the blocked account as a local notification failure,
  writes a replayable terminal idempotency failure, and finishes nonzero without suppressing the clean delivery.
- The post-scan guard freezes only delivery selection/state. Deterministic current Brief persistence still advances,
  while pending and ambiguous delivery bytes remain unchanged in both delivery-only and post-scan paths.
- Quiet-hours handling cannot convert a known local blocker into a successful skip.

### DR-S1-02 — accepted — fixed

All supported v1 lifecycle paths now validate overlay-era revision digests against the exact compatible digest set
derived from the raw revision.

- Reading an existing pointer for the next preparation accepts and preserves its historical digest in the delta key.
- v1 migration preview recognizes the historical pointer digest as valid while keeping new migrated state on the
  current stripped digest contract.
- An unconfirmed overlay-era revision can still be confirmed with its exact historical digest.
- Delta confirmation accepts either exact compatible base-revision digest encoded in the prepared delivery key;
  arbitrary values remain rejected.

### DR-S1-03 — accepted — fixed

The successful-source validator now has one private read-and-validate helper returning both normalized Brief and the
same raw mapping. The retry classifier receives that mapping out of the envelope validation and performs no second
revision read. A counted-read regression locks this boundary.

## Additional cross-account regression

A two-account retry regression proves that one retired `lx` envelope is not sent or mutated while a clean `sy`
envelope is delivered; the overall run still reports the local blocker.

## Validation

```text
209 passed
python -m compileall -q domain src tests: passed
git diff --check: passed
```
