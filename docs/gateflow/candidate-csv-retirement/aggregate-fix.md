# Gateflow Fix Artifact — Aggregate DeepReview

- Gate: `fix`
- Work unit: `candidate-csv-retirement`
- Initial review: `docs/reviews/code-review-20260813-015308.md`
- Status: fixes complete; pending aggregate re-review
- Artifact path: `docs/gateflow/candidate-csv-retirement/aggregate-fix.md`

## Scope and finding decisions

### AGG-CR-01 — accepted — fixed

Agent candidate filter/rank explanations now enter through the same terminal
`candidate_snapshot_manifest.v1` bundle gate as Daily Brief and Advice. Explicit
run lookup validates the manifest, status index, exact owner set, owner file hash,
and current opening contract before exposing facts. Latest lookup resolves one
account run and fails closed when that newest run is incomplete; it does not scan
past a partial run to salvage an older owner snapshot. Agent source metadata now
includes the terminal manifest hash and names the manifest-bound authority.

Regression coverage proves explicit-run missing-manifest failure, latest valid
bundle loading, latest incomplete-run failure without stale fallback, and valid
filter/rank projections from a terminal bundle.

Final status: `已修复`.

### AGG-CR-02 — accepted — fixed

Opening snapshot sealing now derives an exact `(canonical symbol, strategy mode)`
allowlist from scan statuses and rejects every accepted or rejected decision that
escapes it. Full-current snapshot validation independently reconstructs the same
allowlist from strategy scopes and rejects a rehashed cross-scope decision. The
manifest/current-run loader requires the complete current decision and scope
contract; the historical direct loader retains only its explicit read-only v1
compatibility mode.

The Daily Brief test converter was migrated from hand-built minimal snapshot JSON
to the production sealer. This preserves compact CSV fixture syntax without
allowing a test to publish an incomplete object as current terminal authority.

Regression coverage proves both sealer-time rejected cross-symbol failure and
validator-time tampered/rehashed cross-scope failure, while all 52 Daily Brief
scenarios pass through the real sealer and manifest publisher.

Final status: `已修复`.

### AGG-CR-03 — accepted — fixed

The Combo Yield v2 validator now requires top-level
`candidate_owner == "sp_lc"`. A snapshot whose owner is changed and whose content
hash is recomputed is rejected, keeping schema, file owner, top-level identity,
and nested scope ownership consistent.

Final status: `已修复`.

## Documentation decision

- `docs/AGENT_WIKI.md` now states that candidate filter/rank tools consume only a
  terminal manifest-bound opening owner and do not fall back across incomplete
  runs.
- `docs/candidate_strategy.md` now records the terminal manifest's owner/scope
  binding and the shared current-consumer gate.
- `docs/DEPENDENCY_GRAPH.md` was regenerated after the current loader/API and test
  dependency changes.

## Validation

- Focused manifest/opening/Combo/Agent contract suite: `175 passed`.
- Candidate evidence and consumer suite: `421 passed`; its initially stale
  dependency graph was regenerated and the graph tests then passed.
- Daily Brief fixture migration suite: `52 passed`.
- Sandbox-compatible complete repository suite:
  `4789 passed, 10 skipped, 1 deselected`.
- The sole deselected test requires binding a temporary loopback HTTP port and
  passed separately outside the network-restricted sandbox: `1 passed`.
- Full Ruff check over `src domain scripts tests`: pass.
- Compileall over `src domain scripts tests`: pass.
- Generated dependency graph check: pass, 585 production modules and zero cycles.
- US/HK example config validate and build dry-run: pass for all four commands.
- `git diff --check`: pass.

## Residual risks and uncovered areas

- No live OpenD request, notification delivery, runtime artifact rewrite,
  release, deployment, or remote upgrade was performed. These are outside the
  confirmed source-only work unit and require separately authorized operational
  work; classification: `assigned to later work unit`.
- Latest account-run resolution deliberately fails closed on the newest run that
  contains the requested account but lacks a terminal manifest; it never presents
  older facts as current. This is the approved terminal-authority invariant, not
  an uncovered fallback path; classification: `fixed in current slice`.
- Historical pre-manifest v1 loading remains available only through the bounded
  history-classification path and cannot be used by current Agent, Daily Brief, or
  Advice consumers; classification: `fixed in current slice`.
