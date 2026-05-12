## Failure

Using the run-level scheduler decision as the scan gate for every account made
multi-account execution sensitive to shared `last_scan_utc` state. One account
could suppress or drive another account's pipeline run.

## Lesson

In multi-account orchestration, any state that has an account dimension must be
read and written with that account dimension preserved. Global state can be a
fallback, but it should not be the primary scan gate for per-account work.
