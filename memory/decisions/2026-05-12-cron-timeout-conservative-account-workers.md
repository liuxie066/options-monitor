## Context

OpenClaw cron runs have a hard 120s timeout, while multi-account tick can create
nested concurrency: account workers, per-account watchlist workers, and required
data prefetch workers.

## Decision

Default multi-account execution should run account pipelines sequentially unless
`runtime.multi_account_max_workers` or `runtime.account_max_workers` explicitly
opts into account-level parallelism.

Keep symbol-level pipeline parallelism configurable through
`runtime.pipeline_symbol_max_workers`, but avoid multiplying it by account-level
parallelism by default.
