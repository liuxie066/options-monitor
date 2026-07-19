# Code Review — trade-intake-auth-aware-restart slice 2

- Gate: code review / fix / re-review
- Base: `df692615`
- Decision: pass after fix

## Finding

### CR-1 — Medium — one-second global-state polling risks becoming a new OpenD request/log source

The accepted plan proposed one-second polling to bound detection, but `get_global_state()` is an SDK request rather than a local flag. A permanent one-Hz probe is unnecessarily aggressive after startup and could interact with OpenD rate limiting. Fixed by checking immediately after start and then using a fixed five-second interval, still far below the previous 60-second application observation gap and without adding config.

## Re-review

Pass. Terminal auth exits immediately on the first observation; steady-state health checks occur at five-second intervals. Retry delay remains independent and capped at 60 seconds. Multi-source result coordination cannot block indefinitely on sibling join after terminal auth because it observes the queue and sets the shared stop event.

## Validation

- focused listener/auto-intake suites: 19 passed.
- Python compile: passed.

## Residual risks

- SDK may not expose asynchronous auth text in global state: production canary owner.
- five-second polling cost must be observed in production, but is bounded and materially lower than the reviewed one-second plan.
