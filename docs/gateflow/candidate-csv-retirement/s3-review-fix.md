# Gateflow Fix Artifact — S3 DeepReview

- Gate: `fix`
- Work unit: `candidate-csv-retirement`
- Slice: `S3`
- Initial review: `docs/reviews/code-review-20260813-003008.md`
- Re-review: `docs/reviews/code-review-20260813-012542.md`
- Status: complete; re-review accepted
- Artifact path: `docs/gateflow/candidate-csv-retirement/s3-review-fix.md`

## Finding decisions

- `S3-CR-01`: accepted and fixed. Added a 20-case US/HK × SP+LC/CC+LP ×
  enabled/disabled/empty/failure/success-empty filesystem matrix through the real
  watchlist, symbol-monitoring, per-scope status, owner sealing, and terminal manifest
  chain. Strategy calculations use deterministic adapters so the test isolates the
  artifact/state boundary. Every case asserts zero retired candidate CSVs and validates
  the v2 status index plus manifest-bound bundle.
- `S3-CR-02`: accepted and fixed. Account-run Combo trace rows now point to
  `state/combo_yield_candidate_snapshot.json`. Manual/non-account-run execution has no
  terminal owner snapshot, so its trace rows correctly point to
  `candidate_filter_trace.jsonl` instead of inventing a missing sealed artifact.

## Additional hardening during fix

- The static signature guard now checks the exact retired `reject_log_output` parameter.
- Scanner CLI tests now reject the exact retired `--output`, `--reject-log-output`, and
  `--quiet` forms, including the flag-only boolean shape.
- Current operator documentation now describes manifest-bound snapshots plus JSONL trace;
  reject-log and terminal candidate CSV wording remains only where explicitly historical.

## Verification

- Review-fix regression suite:
  `tests/test_candidate_csv_retirement.py`,
  `tests/test_pipeline_capture_status_routing.py`, and
  `tests/test_combo_yield_steps.py`: `67 passed`.
- Complete changed-test suite plus the new static guard: `569 passed`.
- Repository suite excluding the four loopback HTTP cases:
  `4779 passed, 10 skipped, 4 deselected`.
- The four loopback HTTP cases were run outside the network-restricted sandbox:
  `4 passed`.
- Full Ruff, compileall, dependency graph check, and `git diff --check`: pass.

## Residual risks and uncovered areas

- Actual strategy calculations inside the filesystem matrix are replaced by deterministic
  adapters; calculation/reason/order/text invariants remain covered by the focused SP, CC,
  SP+LC, and CC+LP tests and will be rerun before the S3 checkpoint. Classification: fixed
  in current slice by combined boundary plus calculation-focused coverage.
- No live OpenD call, notification delivery, runtime rewrite, release, deployment, or
  historical artifact mutation is part of this source-only slice. Classification: assigned
  to a separately authorized operations work unit and not required for S3 acceptance.
