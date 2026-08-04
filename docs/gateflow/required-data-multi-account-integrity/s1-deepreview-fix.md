# Gateflow Fix — S1 DeepReview Findings

## Gate

- Work unit: `required-data-multi-account-integrity`
- Slice: `S1`
- Gate: `fix`
- Source review: `docs/reviews/code-review-20260804-120035.md`
- Final aggregate re-review: `docs/reviews/code-review-20260804-153946.md`
- Status: complete; final aggregate re-review found zero material issues

## Finding decisions

All seven findings are accepted. Each has direct source evidence or a deterministic
temporary-directory reproduction, affects an approved S1 owner, and is not the
deferred S6 exact-execution implementation or the separately accepted consumer
TOCTOU.

- `DR-S1-01` — accepted: close the expected-contract relation and child-evidence
  invariants.
- `DR-S1-02` — accepted: make declared DTE, position strike, spot, and numeric
  constraints fail closed and actually participate in coverage.
- `DR-S1-03` — accepted: prove the complete raw-to-consumer CSV projection before
  receipt authority.
- `DR-S1-04` — accepted: require one and only one row per requested contract.
- `DR-S1-05` — accepted: make terminal snapshot seal write-once-or-verify.
- `DR-S1-06` — accepted: preserve structured connection causes for gateway health.
- `DR-S1-07` — accepted: bind cache metadata to final CSV bytes and the original
  provider observation time.

## Fix boundary

The fixes remain inside the S1 production allowlist and directly corresponding
required-data tests. `multiplier_steps.py` remains unchanged; its existing enrichment
is coordinated by the finalizer owner. No S2-S7 implementation, production operation,
configuration mutation, notification, release, deployment, merge, or push is included.

## Closure evidence

- Per-finding negative and positive regressions now cover exact child identity,
  DTE/expiration/strike relations, complete raw-to-CSV projection, duplicate
  contract rejection, terminal write-once seal, nested connection evidence,
  and final-byte metadata binding.
- Ready position demands retain expiry-local exact strikes and discovery-day
  DTE. One-sided unbounded strategy windows remain unbounded after union, and
  forged cross-source effective range inversions are rejected by the contract.
- Ordered in-process multi-spec execution uses one gateway and one final
  publication. A failed child retains its original structured payload for
  gateway health and stops later children; duplicate/conflicting contract rows
  cannot be silently hidden before finalization.
- Discovery trading date now owns expiration DTE semantics across contract,
  typed coverage, and debug coverage. Forged DTE ranges and invalid or
  mismatched row DTE values fail closed; legal empty top-level projection
  evidence remains non-executable, and provider-failure semantics remain intact.

## Independent follow-up findings and closure

- `DR-S1-FR-01` — accepted, fixed: aggregate raw and every ordered child now
  exact-bind the expected discovery trading date; both success-empty synthesis
  paths publish that date.
- `DR-S1-FR-02` — accepted, fixed: duplicate snapshot rows are detected within
  a batch, across batches, and during fallback; the conflicted code is removed,
  the result is incomplete, and no last-write-wins value can acquire authority.
- `DR-S1-FR-03` — accepted, fixed: required RV binds meta to every row across
  `realized_volatility_20`, `_60`, `_120`, and `_estimate`; explicit `None` is
  preserved for unavailable windows, while missing, boolean, non-finite,
  negative, or inconsistent evidence fails closed.
- `DR-S1-FR-04` — accepted, fixed: provider timestamps must be timezone-aware;
  naive values are rejected rather than assigned UTC.
- `DR-S1-FR-05` — accepted, fixed: raw/policy/receipt/manifest physical ports
  must be non-boolean integers; floats are not truncated.
- `DR-S1-FR-06` — accepted, fixed: cached finalization uses the existing owner
  instead of duplicating raw-path logic, and snapshot loading no longer imports
  the Close Advice plan owner. Seal/reseal binds attachment bytes/hash; the
  Close Advice resolver retains typed semantic validation.
- `DR-S1-FR-07` — accepted, fixed: when the expected contract requires RV, each
  ordered child must carry successful canonical 20/60/120/estimate evidence
  exactly matching the aggregate and rows. Missing, failed, non-canonical, or
  drifting child RV now fails before finalizer persistence and before direct
  receipt publication.

- Final changed-file S1 suite: `508 passed`.
- Final child-RV/provider/finalizer subset: `128 passed`.
- Independent post-fix provider re-review: zero material residual; child-RV
  cases `9 passed` and real multi-spec merge/finalize `1 passed`.
- Earlier strict cross-contract subset: `278 passed`; an independent contract
  audit passed `274` tests before `DR-S1-FR-07` was found by the final provider
  audit. The changed-file and child-RV sets above include its fix.
- Broad non-environment suite after `DR-S1-FR-07`: `4199 passed, 10 skipped`;
  the excluded CLI-only
  files require a worktree-local `.venv/bin/python`, which this isolated
  worktree intentionally does not have.
- `python3 -m compileall -q` over all changed S1 production owners: pass.
- Ruff over all changed S1 Python owners and direct tests: pass.
- `git diff --check`: pass.
- Dependency graph: current, `577` production modules, `0` cycles; architecture
  and raw-fetch guards `3 passed`.

The new timestamped aggregate DeepReview found zero material issues. S1 is
ready for its protected local commit on the accepted lineage.
