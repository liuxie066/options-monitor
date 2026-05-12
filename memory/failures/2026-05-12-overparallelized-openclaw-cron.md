## Failure

The tick efficiency optimization allowed multiple account pipelines to run at
the same time. Each account pipeline can already process several watchlist
symbols concurrently, so the effective OpenD/Futu pressure multiplied under
multi-account cron.

## Lesson

For OpenClaw cron, lower wall-clock time is not just more workers. Nested worker
pools can overload the shared data source, make individual calls slower, and hit
the 120s cron timeout.
