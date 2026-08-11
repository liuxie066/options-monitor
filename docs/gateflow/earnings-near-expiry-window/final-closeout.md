# Gateflow Final Closeout — Earnings Near-Expiry Window

- Gate: `final closeout`
- Work unit: `earnings-near-expiry-window`
- Date: 2026-08-11
- Branch: `feat/earnings-near-expiry-window`
- Draft PR: https://github.com/liuxie066/options-monitor/pull/148
- Status: `implementation completed; awaiting user-authorized merge`

## What Changed

- Sell Put, Covered Call, and Combo Yield Funding Put now share one earnings policy: an event is blocking only when
  its market-local natural date falls in the inclusive `expiration - 6 days .. expiration` window. Day 0 and day 6
  block; day 7 and earlier are non-blocking context.
- The scan date only bounds evidence collection for contracts that are still openable. A same-day event remains
  pending for the entire market-local date, as explicitly confirmed for this work unit.
- OpenD gaps fail closed only when they overlap the hard window and leave that contract's outcome unresolved. A
  known blocker is conclusive even if another interval failed; fully evidenced candidates survive with a
  partial-universe warning.
- Zero accepted candidates plus an unresolved hard-window evidence gap blocks substantive AI Advice instead of
  being presented as a clean `no_candidate` result.

## Candidate Authority and Retained CSVs

- Sell Put and Covered Call opening authority is `state/opening_candidate_snapshot.json`.
- Combo Yield authority is `state/combo_yield_candidate_snapshot.json`.
- Candidate decision, Advice, and Daily Brief consume the sealed JSON snapshots; they do not reread candidate CSVs
  as decision authority.
- CSV remains for parsed market data, audit/report/history, Close Advice, research, and Shadow Replay compatibility.
  The compatibility-named Combo put-universe CSV is now an enriched audit universe; Candidate Engine is the sole
  capacity-filter owner. Complete CSV retirement is a separate work unit.

## Findings Fixed

- Combo Funding Put hard-window evidence gaps now remain unavailable through decision sinks, sealed Combo JSON,
  Advice, and Daily Brief instead of collapsing to clean no-candidate.
- Combo cash handling no longer removes contracts before Candidate Engine or hides their evidence diagnostics.
- Opening snapshot validation rejects malformed collection rows at the contract boundary instead of leaking an
  `AttributeError`.
- Candidate-universe completeness no longer treats non-benign `not_applicable` results as complete and includes
  outcome-unresolved contract scopes.

## What Was Verified

- Final affected strategy chain: **265 passed**.
- Snapshot, Advice, and Daily Brief focus: **106 passed**.
- Full repository in sandbox: **4717 passed, 10 skipped**, plus one localhost bind permission failure caused by the
  sandbox. The exact read-only localhost HTTP test passed outside the sandbox: **1 passed**.
- `ruff check domain src tests`: pass.
- `compileall domain src`: pass.
- Dependency graph: **585 modules, 0 cycles**.
- `git diff --check`: pass.
- PlanReview, clean slice review, aggregate DeepReview, and PR-level DeepReview are complete. Four accepted slice
  findings were fixed; the final slice, aggregate, and PR reviews found no additional findings.
- Hosted checks passed on the implementation/aggregate-reviewed PR head. On the later documentation-only PR-review
  head, agent-plugin, guardrails, Analyze (python), and the aggregate CodeQL check passed. The CodeQL workflow and
  every `Analyze (actions)` step report success, while that individual check-run's metadata remains transiently
  `in_progress`; this is a GitHub status-finalization inconsistency, not a failed check.

## Remaining Risks / Owners

- OpenD does not provide a per-symbol completeness proof for the earnings calendar. The implemented exact-window
  interval coverage is the confirmed policy, but provider omission remains a provider-contract risk.
- No live production tick or live notification was run. Runtime verification belongs to a separately authorized
  release/upgrade step.
- Full retirement or renaming of retained CSV outputs belongs to a separate compatibility-cleanup work unit.

## Issue Link Status

- This is not an issue-driven work unit. The PR body contains no closing keyword and no issue closeout comment is
  required.

## Next Entry Point

- Review Draft PR #148. Marking ready, merging, releasing, upgrading a runtime, service changes, production writes,
  and notification replay are separate authorities and were not performed.
