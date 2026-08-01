# OM OpenD Runtime Boundary Fix Plan

## Goal

Close the two production Options Monitor failures with the smallest end-to-end repair:

1. Strategy Lab OpenD sampling must use the service profile's runtime-owned cache and rate-limit state instead of the immutable release directory.
2. Strategy Lab must inherit the same OpenD endpoint limits as the configured production pipelines, so one shared state file is governed by one limit contract.
3. Generated systemd one-shot services with an execution bound must use `TimeoutStartSec`, which systemd enforces for `Type=oneshot`, instead of the ignored `RuntimeMaxSec` directive.
4. After release and remote upgrade, Position Advice promotion and Strategy Lab sampling must both complete under their intended fail-closed contracts.

## Observed Evidence

- The production profile resolves `runtime_root=/var/lib/options-monitor`, but Strategy Lab passes the release repository root into `run_strategy_lab_update()` and ultimately into `execute_required_data_opend()`.
- Production has two limiter files:
  - `/var/lib/options-monitor/output_shared/state/opend_option_chain_limiter.json` with configured `max_calls=9`;
  - `/home/liuxie/apps/releases/1.8.2/output_shared/state/opend_option_chain_limiter.json` with Strategy Lab's default `max_calls=10`.
- The production market configs declare `runtime.opend_rate_limits.option_chain.max_calls=9`, `window_sec=30`, and `max_wait_sec=600`.
- The generated Position Advice promotion and Strategy Lab sample units are `Type=oneshot` but contain `RuntimeMaxSec=600`; systemd reports that this setting is ignored for one-shot services.
- A current read-only Position Advice promotion refresh succeeds on v1.8.2 and isolates incompatible legacy plans. Its recorded failed unit state predates the current code and needs a post-upgrade controlled run, not another business-rule change.
- PR #127 has already merged the first-rate-limit circuit breaker and stock-refresh evidence classification into `main` at `c3853c08`.

## Scope and Contracts

### S1 - Runtime-owned OpenD state and configuration

- Resolve the OpenD runtime base from the Strategy Lab CLI's existing `--runtime-root` / service profile resolution.
- Pass that base independently from `repo_root` through Strategy Lab update, Shadow Replay data-plan, and mark collection into `execute_required_data_opend()`.
- Keep repository-root semantics unchanged for datasets, source runs, and code-owned paths.
- Load OpenD fetch limits from every service-profile config path and combine them conservatively for a cross-market sampler:
  - minimum `*_max_calls`;
  - maximum `*_window_sec`;
  - minimum `*_max_wait_sec`.
- If a supplied profile declares config paths, fail with `CONFIG_ERROR` when any referenced config is missing, unreadable, invalid JSON, or not an object; never silently reintroduce library defaults for a broken production profile.
- Preserve existing defaults only when no profile config path is available. Explicit function callers remain backward compatible.
- Dry-runs continue to use temporary OpenD state/cache roots and persist nothing.

### S2 - Enforced systemd one-shot execution bounds

- Replace the generator's `runtime_max_sec` option with `timeout_start_sec`.
- Render `TimeoutStartSec=<seconds>` only for currently bounded one-shot services.
- Preserve the existing 600/300 second values and negative assertions for unbounded/long-running units.
- Update operator documentation to describe the enforced start timeout accurately.

### S3 - Delivery and production closure

- Add focused regression tests for CLI profile/root resolution, parameter propagation, persistent-vs-dry-run OpenD base selection, conservative limit merging, and generated unit directives.
- Run targeted tests, Ruff, dependency-graph regeneration/check if imports change, and the complete test suite.
- Run DeepReview for the implementation and for the pull request; fix all high/critical findings and all accepted medium findings.
- Merge to `main`, publish the next project version through the repository release workflow, then upgrade the authorized remote through the controlled updater.
- Stop the stale pre-upgrade Strategy Lab one-shot before upgrade if it is still active.
- Re-run both production services and verify:
  - Position Advice promotion exits successfully, publishes only promotion evidence/archive state, remains `v2_shadow`, and performs no final Position Advice CAS or notifications;
  - Strategy Lab exits successfully as updated or deferred, emits at most one OpenD rate-limit outcome per run, stays within the enforced timeout, and creates or modifies limiter/cache state only under `/var/lib/options-monitor`;
  - the retained v1.8.2 release-tree limiter artifact is unchanged across the canary; no deletion is required;
  - timers remain enabled/active and neither unit remains failed.

## Non-goals

- No Position Advice business-rule rewrite, final advice CAS, notification, broker, trade, or runtime-config write.
- No deletion of legacy promotion archives or the stale limiter file in the old v1.8.2 release directory.
- No global OpenD architecture refactor and no change to unrelated service schedules.
- No automatic release/apply beyond the explicitly authorized remote environment.

## Validation Matrix

| Risk | Test / Evidence |
|---|---|
| Strategy Lab still writes into a release directory | CLI and collection tests assert `opend_base_root` equals runtime root for writes and a temporary path for dry-runs; production pre/post mtime evidence proves the new release tree is untouched. |
| Sampler uses a conflicting limit contract | Profile tests use two market configs with differing limits and assert conservative merged fetch kwargs reach the update service; missing or malformed profile configs fail with `CONFIG_ERROR`. |
| Existing callers regress | Direct Strategy Lab / Shadow Replay tests without the new optional arguments retain prior behavior. |
| One-shot timeout remains ineffective | Render tests assert `TimeoutStartSec` on every currently bounded one-shot and no `RuntimeMaxSec`; long-running services remain unbounded. |
| Rate-limit storms recur | Existing first-rate-limit circuit-breaker tests plus a production run verify one rate-limit outcome followed by deferred work. |
| Production mutation exceeds authority | Inspect service command, receipts/artifacts, unit journal, notification evidence, and advice authority after the canaries. |

## Rollback

- Code rollback: revert the scoped merge and publish a follow-up release; the old release remains immutable.
- Runtime rollback: use the controlled updater to return the stable symlink to v1.8.2 and reconcile units, then verify timers and service status.
- Evidence written by promotion/Strategy Lab is append-only/local runtime evidence and is not deleted during rollback.

## Acceptance

- All repository gates pass and DeepReview has no unresolved high/critical finding.
- The next release tag and GitHub Release are successful and point to the intended release commit.
- Remote reports the new version, rendered units contain effective start timeouts, and service drift is clean.
- Both named production services complete under the contracts above, with no unauthorized external side effects.
